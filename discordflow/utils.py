import asyncio
import audioop
import io
import logging
import time
import unicodedata
import wave
from contextlib import suppress, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial, wraps

import simpleaudio
import yaml

logger = logging.getLogger(__name__)
language = ContextVar('language', default='ru')


def _(s):
    # TODO: here should be localization
    return s


class Interrupted(Exception):
    pass


class EmptyUtterance(Exception):
    pass


class TooLongUtterance(Exception):
    pass


class BackgroundTask:
    def __init__(self):
        self.task = None

    async def wrap(self, coro):
        try:
            await coro
        except Exception as exc:
            logger.exception(f"Unexpected exception in {coro}: {exc!r}")
            raise

    def start(self, coro):
        if self.task is not None:
            raise RuntimeError("{self!r} is already running")
        self.task = asyncio.create_task(self.wrap(coro))

    async def stop(self):
        task, self.task = self.task, None
        if task.done():
            return task.result()
        else:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@asynccontextmanager
async def background_task(coro):
    try:
        task = BackgroundTask()
        task.start(coro)
        yield
    finally:
        await task.stop()


class Waiter(BackgroundTask):
    def set(self, delay, callback):
        self.start(self.wait(delay, callback))

    async def wait(self, delay, callback):
        await asyncio.sleep(delay)
        await callback()


async def sync_to_async(func, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(None, partial(func, *args, **kwargs))


@dataclass
class Audio:
    channels: int
    width: int
    rate: int
    data: bytes = b''

    def __str__(self):
        duration = round(self.duration, 3)
        return f'channels={self.channels} width={self.width} rate={self.rate}Hz frames={len(self)} duration={duration}s'

    def __repr__(self):
        return f'{type(self)}[{self}]'

    def __add__(self, other):
        if other.channels != self.channels or other.width != self.width or other.rate != self.rate:
            raise ValueError("Could not add incompatible Audio")
        return self.clone(self.data + other.data)

    def __mul__(self, factor):
        return self.clone(audioop.mul(self.data, self.width, factor))

    def combine(self, other):
        return self.clone(data=audioop.add(self.data, other.data, self.width))

    def clone(self, data):
        """Return clone with different data"""
        return Audio(width=self.width, channels=self.channels, rate=self.rate, data=data)

    def __getitem__(self, slice):
        """Slice frames"""
        start = slice.start and slice.start * self.framewidth
        stop = slice.stop and slice.stop * self.framewidth
        step = slice.step and slice.step * self.framewidth
        return self.clone(self.data[start:stop:step])

    def __len__(self) -> int:
        """Audio length in frames"""
        return len(self.data) // self.framewidth

    def __bool__(self) -> bool:
        return len(self) > 0

    @property
    def duration(self) -> float:
        """Duration in seconds"""
        return len(self) / self.rate

    @property
    def framewidth(self) -> int:
        return self.channels * self.width

    @property
    def rms(self) -> float:
        return audioop.rms(self.data, self.width)

    def clear(self):
        self.data = b''

    def to_mono(self):
        if self.channels == 1:
            return self
        elif self.channels == 2:
            return Audio(channels=1, width=self.width, rate=self.rate, data=audioop.tomono(self.data, self.width, 0.5, 0.5))
        else:
            raise ValueError(f"Can't convert audio with channels={self.channels}")

    def to_stereo(self):
        if self.channels == 2:
            return self
        elif self.channels == 1:
            return Audio(channels=2, width=self.width, rate=self.rate, data=audioop.tostereo(self.data, self.width, 0.5, 0.5))
        else:
            raise ValueError(f"Can't convert audio with channels={self.channels}")

    def to_rate(self, rate) -> 'Audio':
        converted, _ = audioop.ratecv(self.data, self.width, self.channels, self.rate, rate, None)
        return Audio(channels=self.channels, width=self.width, rate=rate, data=converted)

    @classmethod
    def load(cls, fp: str) -> 'Audio':
        with wave.open(fp, 'rb') as f:
            return Audio(
                data=f.readframes(2 ** 32),
                channels=f.getnchannels(),
                width=f.getsampwidth(),
                rate=f.getframerate(),
            )

    @classmethod
    def from_wav(cls, wav: bytes) -> 'Audio':
        return cls.load(io.BytesIO(wav))

    def to_wav(self) -> io.BytesIO:
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as f:
            f.setnchannels(self.channels)
            f.setsampwidth(self.width)
            f.setframerate(self.rate)
            f.writeframes(self.data)
        wav.seek(0)
        return wav

    def silence(self, frames: int) -> 'Audio':
        return self.clone(b'\x00' * (frames * self.framewidth))

    def play(self):
        """Useful for debugging"""
        play = simpleaudio.play_buffer(self.data, num_channels=self.channels, bytes_per_sample=self.width, sample_rate=self.rate)
        try:
            play.wait_done()
        except KeyboardInterrupt:
            play.stop()


class UserState:
    __slots__ = ['values', 'filepath']

    def __init__(self, skill, ctx):
        self.values = {}
        self.filepath = f'skill-states/{skill}.{ctx.channel.id}.{ctx.user.name}.yaml'

    def __setattr__(self, name, value):
        if name in self.__slots__:
            super().__setattr__(name, value)
        else:
            self.values[name] = value
            self.save()

    def __getattr__(self, name):
        return self.values.get(name)

    def load(self):
        with open(self.filepath, 'r') as f:
            self.values = yaml.load(f)

    def save(self):
        with open(self.filepath, 'w') as f:
            return yaml.dump(self.values, f)


class Registry:
    def __init__(self):
        self.skills = {}
        self.initializers = {}

    def skill(self, name=None, init=None):
        def decorator(func):
            skill_name = name or func.__name__
            self.skills[skill_name] = func
            logger.info(f"Loaded skill '{skill_name}'")
            return func
        if init:
            self.initializers[name] = init
        return decorator

    def initialize_skills(self):
        for skill in list(self.initializers):
            self.initialize(skill)

    def initialize(self, skill: str):
        logger.debug(f"Initializing {skill}")
        self.initializers[skill]()
        del self.initializers[skill]

    async def run_skill(self, skill: str, ctx, *args, **kwargs):
        if skill in self.skills:
            if skill in self.initializers:
                self.initialize(skill)
            with self.acquire_state(skill, ctx) as state:
                await self.skills[skill](ctx, state, *args, **kwargs)
        else:
            await ctx.say("такого я не умею")

    @contextmanager
    def acquire_state(self, skill: str, ctx):
        state = UserState(skill, ctx)
        try:
            state.load()
        except Exception as exc:
            logger.debug(f"Cant load state for {skill}.{ctx} due to {exc}")
        try:
            yield state
        finally:
            state.save()


registry = Registry()


async def aiter(iter_):
    for i in iter_:
        yield i


async def size_limit(audio_iter, size):
    buf = None
    async for packet in audio_iter:
        buf = buf + packet if buf else packet
        while len(buf) >= size:
            yield buf[:size]
            buf = buf[size:]
    if buf:
        yield buf


async def rate_limit(audio_iter):
    when_to_wake = time.perf_counter()
    async for packet in audio_iter:
        yield packet
        when_to_wake += packet.duration
        to_sleep = when_to_wake - time.perf_counter()
        await asyncio.sleep(to_sleep)
    if packet:
        yield packet


def strip_accents(s: str):
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


@asynccontextmanager
async def aclosing(generator):
    try:
        yield generator
    finally:
        await generator.aclose()


class AsyncTimedIterator:
    def __init__(self, iterable, timeout, sentinel):
        self._iterable = iterable
        self.timeout = timeout
        self._sentinel = sentinel
        self.task = None

    def __aiter__(self):
        self._iterator = self._iterable.__aiter__()
        return self

    async def __anext__(self):
        task = self.task or asyncio.create_task(self._iterator.__anext__())
        self.task = None
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except asyncio.TimeoutError:
            self.task = task
            return self._sentinel


class CancellableAsyncGenerator:
    def __init__(self, gen):
        self.gen = gen

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, cb):
        await self.aclose()

    def __aiter__(self):
        return self

    async def aclose(self):
        logger.debug("aclose gen")
        if self.gen is None:
            return
        task = self.task or asyncio.create_task(self.gen.__anext__())
        self.task = None
        task.cancel()
        self.gen = None
        with suppress(asyncio.CancelledError):
            await task

    async def __anext__(self):
        if self.gen:
            self.task = asyncio.create_task(self.gen.__anext__())
            try:
                return await self.task
            except asyncio.CancelledError:
                if self.gen is None:
                    raise StopAsyncIteration
                else:
                    raise
            finally:
                self.task = None
        else:
            raise StopAsyncIteration


def cancellable_stream(afunc):
    @wraps(afunc)
    def wrapper(*args, **kwargs):
        return CancellableAsyncGenerator(afunc(*args, **kwargs))
    return wrapper
