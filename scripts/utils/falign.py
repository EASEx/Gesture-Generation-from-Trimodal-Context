from dataclasses import dataclass
import torch
import torchaudio
from typing import List, Tuple

@dataclass
class Point:
    token_index: int
    time_index: int
    score: float

@dataclass
class Segment:
    label: str
    start: int
    end: int
    score: float

    def __repr__(self):
        return f"{self.label}\t({self.score:4.2f}): [{self.start:5d}, {self.end:5d})"

    @property
    def length(self):
        return self.end - self.start

@dataclass
class Word:
    data: str
    start: float
    end: float


class Wave2Vec2Aligner():
    def __init__(self) -> None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
        self.model = bundle.get_model().to(device)
        self.labels = bundle.get_labels()
        self.dictionary = {c: i for i, c in enumerate(self.labels)}

    def get_trellis(self, emission, tokens, blank_id=0):
        num_frame = emission.size(0)
        num_tokens = len(tokens)

        # Trellis has extra diemsions for both time axis and tokens.
        # The extra dim for tokens represents <SoS> (start-of-sentence)
        # The extra dim for time axis is for simplification of the code.
        trellis = torch.empty((num_frame + 1, num_tokens + 1))
        trellis[0, 0] = 0
        trellis[1:, 0] = torch.cumsum(emission[:, 0], 0)
        trellis[0, -num_tokens:] = -float("inf")
        trellis[-num_tokens:, 0] = float("inf")

        for t in range(num_frame):
            trellis[t + 1, 1:] = torch.maximum(
                # Score for staying at the same token
                trellis[t, 1:] + emission[t, blank_id],
                # Score for changing to the next token
                trellis[t, :-1] + emission[t, tokens],
            )
        return trellis

    def backtrack(self, trellis: torch.Tensor, emission: torch.Tensor, tokens: list, blank_id=0) -> "list[Point]":
        # Note:
        # j and t are indices for trellis, which has extra dimensions
        # for time and tokens at the beginning.
        # When referring to time frame index `T` in trellis,
        # the corresponding index in emission is `T-1`.
        # Similarly, when referring to token index `J` in trellis,
        # the corresponding index in transcript is `J-1`.
        j = trellis.size(1) - 1
        t_start = torch.argmax(trellis[:, j]).item()

        path = []
        for t in range(t_start, 0, -1):
            # 1. Figure out if the current position was stay or change
            # Note (again):
            # `emission[J-1]` is the emission at time frame `J` of trellis dimension.
            # Score for token staying the same from time frame J-1 to T.
            stayed = trellis[t - 1, j] + emission[t - 1, blank_id]
            # Score for token changing from C-1 at T-1 to J at T.
            changed = trellis[t - 1, j - 1] + emission[t - 1, tokens[j - 1]]

            # 2. Store the path with frame-wise probability.
            prob = emission[t - 1, tokens[j - 1] if changed > stayed else 0].exp().item()
            # Return token index and time index in non-trellis coordinate.
            path.append(Point(j - 1, t - 1, prob))

            # 3. Update the token
            if changed > stayed:
                j -= 1
                if j == 0:
                    break
        else:
            raise ValueError("Failed to align")
        return path[::-1]

    def merge_repeats(self, transcript:str, path: List[Point]) -> List[Segment]:
        i1, i2 = 0, 0
        segments = []
        while i1 < len(path):
            while i2 < len(path) and path[i1].token_index == path[i2].token_index:
                i2 += 1
            score = sum(path[k].score for k in range(i1, i2)) / (i2 - i1)
            segments.append(
                Segment(
                    transcript[path[i1].token_index],
                    path[i1].time_index,
                    path[i2 - 1].time_index + 1,
                    score,
                )
            )
            i1 = i2
        return segments

    def merge_words(self, segments: List[Segment], separator="|") -> List[Segment]:
        words = []
        i1, i2 = 0, 0
        while i1 < len(segments):
            if i2 >= len(segments) or segments[i2].label == separator:
                if i1 != i2:
                    segs = segments[i1:i2]
                    word = "".join([seg.label for seg in segs])
                    score = sum(seg.score * seg.length for seg in segs) / sum(seg.length for seg in segs)
                    words.append(Segment(word, segments[i1].start, segments[i2 - 1].end, score))
                i1 = i2 + 1
                i2 = i1
            else:
                i2 += 1
        return words

    def align_audio(self, speech_file: str, transcript: str) -> List[Word]:
        result = []
        with torch.inference_mode():
            waveform, _ = torchaudio.load(speech_file)
            emissions, _ = self.model(waveform.to(self.device))
            emissions = torch.log_softmax(emissions, dim=-1)

        emission = emissions[0].cpu().detach()
        tokens = [self.dictionary[c] for c in transcript]
        trellis = self.get_trellis(emission, tokens)
        path = self.backtrack(trellis, emission, tokens)
        segments = self.merge_repeats(transcript, path)
        word_segments = self.merge_words(segments)
        ratio = waveform.size(1) / (trellis.size(0) - 1)
        for word in word_segments:
            x0 = int(ratio * word.start)
            x1 = int(ratio * word.end)
            result.append(Word(word.label, x0 / self.bundle.sample_rate, x1 / self.bundle.sample_rate))
        return result

            









