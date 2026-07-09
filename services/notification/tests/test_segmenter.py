"""Behavior of SMS segment counting (vendored from HW3 — reused domain logic).

`min_sms_segments` is a pure function, so its output for a given input is its
behavior. Tests assert the observable contract (counts, boundaries,
termination), not how it is computed.
"""
import concurrent.futures

from services.notification.src.segmenter import MAX_SEGMENT_CHARS, min_sms_segments


def run_with_timeout(fn, timeout=2.0):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn).result(timeout=timeout)



def test_empty_or_blank_needs_no_segments():
    assert min_sms_segments("") == 0
    assert min_sms_segments("   ") == 0


def test_short_message_fits_in_one_segment():
    assert min_sms_segments("hello world") == 1


def test_message_at_the_limit_stays_one_segment():
    msg = "a" * MAX_SEGMENT_CHARS  # exactly the limit
    assert min_sms_segments(msg) == 1


def test_content_spanning_two_segments_needs_two():
    word = "a" * (MAX_SEGMENT_CHARS - 20)
    # two such words + a space cannot share one segment
    assert min_sms_segments(f"{word} {word}") == 2


def test_more_content_never_needs_fewer_segments():
    # few but long words so the property holds across a segment boundary without depending
    # on word count (large word counts are covered by the dedicated performance test)
    one_word = "a" * (MAX_SEGMENT_CHARS - 10)
    small = min_sms_segments(one_word)
    large = min_sms_segments(f"{one_word} {one_word}")
    assert large >= small >= 1


def test_word_longer_than_a_segment_still_counts():
    # a single word that cannot fit in one segment must still produce a usable count
    assert min_sms_segments("z" * (MAX_SEGMENT_CHARS + 40)) >= 1


def test_long_message_is_computed_quickly():
    # guards against the exponential-time regression: many words must not hang
    msg = "ab " * 300
    result = run_with_timeout(lambda: min_sms_segments(msg), timeout=2.0)
    assert result >= 1
