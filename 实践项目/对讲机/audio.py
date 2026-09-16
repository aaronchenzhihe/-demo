"""Realtime microphone PCM capture/cache/update for QuecPython intercom projects.

This module focuses on:
1) Reading raw PCM from MIC via Audio.PCM.read(size)
2) Re-assembling stable 20ms frames
3) Caching latest frames and printing runtime stats for standalone verification
"""

try:
	from audio import Audio
except Exception:
	Audio = None

try:
	import utime as time
except Exception:
	import time


class AudioFrame:
	"""One PCM frame with metadata."""

	def __init__(self, seq, timestamp_ms, sample_rate, channels, payload):
		self.seq = seq & 0xFFFF
		self.timestamp_ms = timestamp_ms & 0xFFFFFFFF
		self.sample_rate = sample_rate
		self.channels = channels
		self.sample_width = 2
		self.payload = payload

	@property
	def duration_ms(self):
		bytes_per_ms = self.sample_rate * self.channels * self.sample_width / 1000
		if bytes_per_ms <= 0:
			return 0
		return int(len(self.payload) / bytes_per_ms)


class MicPcmReader:
	"""Read microphone PCM and output fixed-length frames."""

	def __init__(
		self,
		mic_dev=2,
		channels=1,
		sample_rate=8000,
		frame_ms=20,
		periodcnt=3,
		blocking=True,
		read_buf_frames=3,
	):
		if Audio is None:
			raise RuntimeError("audio module unavailable")

		self.mic_dev = mic_dev
		self.channels = channels
		self.sample_rate = sample_rate
		self.frame_ms = frame_ms
		self.periodcnt = periodcnt
		self.blocking = blocking
		self.read_buf_frames = read_buf_frames

		self.sample_width = 2
		self.bytes_per_frame = int(
			(self.sample_rate * self.channels * self.sample_width * self.frame_ms) / 1000
		)
		if self.bytes_per_frame <= 0:
			raise ValueError("bytes_per_frame <= 0")

		self._pcm = None
		self._cache = bytearray()
		self._seq = 0

	def open(self):
		mode = Audio.PCM.BLOCK if self.blocking else Audio.PCM.NOBLOCK
		flag = Audio.PCM.READONLY
		self._pcm = Audio.PCM(
			self.mic_dev,
			Audio.PCM.MONO if self.channels == 1 else Audio.PCM.STEREO,
			self.sample_rate,
			flag,
			mode,
			self.periodcnt,
		)
		return self

	def close(self):
		pcm = self._pcm
		self._pcm = None
		self._cache = bytearray()
		if pcm is None:
			return

		# Keep compatibility: some firmware exposes close(), some does not.
		close_fn = getattr(pcm, "close", None)
		if callable(close_fn):
			close_fn()

	def _ticks_ms(self):
		ticks_ms = getattr(time, "ticks_ms", None)
		if callable(ticks_ms):
			return ticks_ms()
		return int(time.time() * 1000)

	def _sleep_ms(self, ms):
		sleep_ms = getattr(time, "sleep_ms", None)
		if callable(sleep_ms):
			sleep_ms(ms)
			return
		time.sleep(ms / 1000.0)

	def read_chunk(self, size=None):
		if self._pcm is None:
			raise RuntimeError("reader not opened")

		if size is None:
			size = self.bytes_per_frame * self.read_buf_frames

		data = self._pcm.read(size)
		if data == -1 or data is None:
			return None
		return data

	def read_frame(self, max_wait_ms=100):
		start_ms = self._ticks_ms()

		while len(self._cache) < self.bytes_per_frame:
			chunk = self.read_chunk()
			if chunk:
				self._cache.extend(chunk)
				continue

			now_ms = self._ticks_ms()
			if (now_ms - start_ms) >= max_wait_ms:
				return None
			self._sleep_ms(2)

		payload = bytes(self._cache[: self.bytes_per_frame])
		del self._cache[: self.bytes_per_frame]

		frame = AudioFrame(
			seq=self._seq,
			timestamp_ms=self._ticks_ms(),
			sample_rate=self.sample_rate,
			channels=self.channels,
			payload=payload,
		)
		self._seq = (self._seq + 1) & 0xFFFF
		return frame


class MicRealtimeCache:
	"""Keep a ring buffer of latest frames and update stats in realtime."""

	def __init__(self, reader, max_frames=20):
		self.reader = reader
		self.max_frames = max_frames if max_frames > 0 else 1
		self.frames = []
		self.total_frames = 0
		self.dropped_frames = 0

	def update_once(self):
		frame = self.reader.read_frame()
		if frame is None:
			self.dropped_frames += 1
			return None

		self.frames.append(frame)
		if len(self.frames) > self.max_frames:
			del self.frames[0]

		self.total_frames += 1
		return frame

	def latest(self):
		if not self.frames:
			return None
		return self.frames[-1]

	def pop_oldest(self):
		if not self.frames:
			return None
		frame = self.frames[0]
		del self.frames[0]
		return frame

	def buffered_count(self):
		return len(self.frames)

	def run(self, duration_ms=None, stop_check=None):
		start = self.reader._ticks_ms()
		while True:
			if stop_check and stop_check():
				break

			if duration_ms is not None:
				if (self.reader._ticks_ms() - start) >= duration_ms:
					break

			self.update_once()


def pcm16le_peak(payload):
	"""Return absolute peak of 16-bit little-endian PCM sample values."""
	if not payload:
		return 0

	peak = 0
	limit = len(payload) - (len(payload) % 2)
	idx = 0
	while idx < limit:
		lo = payload[idx]
		hi = payload[idx + 1]
		value = (hi << 8) | lo
		if value >= 32768:
			value -= 65536
		if value < 0:
			value = -value
		if value > peak:
			peak = value
		idx += 2
	return peak


def build_default_reader(sample_rate=8000, channels=1, frame_ms=20):
	"""Factory with typical intercom defaults: 8k, mono, 20ms frame."""
	return MicPcmReader(
		mic_dev=2,
		channels=channels,
		sample_rate=sample_rate,
		frame_ms=frame_ms,
		periodcnt=3,
		blocking=True,
		read_buf_frames=3,
	)


def standalone_verify(duration_ms=5000, print_interval_frames=10):
	"""Standalone capture test: realtime get + cache update + print stats."""
	reader = build_default_reader()
	reader.open()
	cache = MicRealtimeCache(reader, max_frames=30)

	try:
		start = reader._ticks_ms()
		while (reader._ticks_ms() - start) < duration_ms:
			frame = cache.update_once()
			if frame is None:
				continue

			if (cache.total_frames % print_interval_frames) == 0:
				peak = pcm16le_peak(frame.payload)
				print(
					"[mic] seq=%d ts=%d bytes=%d dur=%dms peak=%d buffered=%d drop=%d"
					% (
						frame.seq,
						frame.timestamp_ms,
						len(frame.payload),
						frame.duration_ms,
						peak,
						cache.buffered_count(),
						cache.dropped_frames,
					)
				)
	finally:
		reader.close()

	print(
		"[mic] done total=%d drop=%d buffered=%d"
		% (cache.total_frames, cache.dropped_frames, cache.buffered_count())
	)
	return cache


if __name__ == "__main__":
	# Default 5s standalone check. Adjust as needed.
	standalone_verify(duration_ms=50000, print_interval_frames=10)
