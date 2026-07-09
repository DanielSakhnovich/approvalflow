# SMS messages are limited to 160 characters per segment (GSM-7).
MAX_SEGMENT_CHARS = 160


def min_sms_segments(message):
    """Minimum number of SMS segments needed to deliver `message`
    without splitting any word across segments. Used to report how
    many billable SMS parts a notification will consume.

    Bottom-up DP: dp[i] = min segments to send words[i:]. Each segment greedily packs
    words (plus the joining space) up to MAX_SEGMENT_CHARS. A word longer than a whole
    segment is placed in its own segment.
    """
    if not message or not message.strip():
        return 0
    words = message.split()
    if not words:
        return 0

    n = len(words)
    dp = [float("inf")] * (n + 1)
    dp[n] = 0

    for i in range(n - 1, -1, -1):
        length = 0
        for j in range(i, n):
            add = len(words[j]) if j == i else len(words[j]) + 1
            if length + add > MAX_SEGMENT_CHARS:
                if j == i:
                    # single word longer than a segment: force it onto its own segment
                    dp[i] = 1 + dp[i + 1]
                break
            length += add
            dp[i] = min(dp[i], 1 + dp[j + 1])

    return dp[0] if dp[0] != float("inf") else 0
