"""Property-based tests: decoding must not depend on how the sender's mailer encoded the subject."""
import base64
from email import quoprimime

from hypothesis import given
from hypothesis import strategies as st

from mailcollect.headers import CATEGORIES, classify, parse_headers
from tests.mailfactory import message, top_lines

PREFIXES = ["[kt cloud 작업 공지]", "[kt cloud 긴급 작업 공지]", "RE: [kt cloud 작업 공지]", "[KT Cloud 작업 공지]"]
chars = st.one_of(
    st.characters(min_codepoint=0xAC00, max_codepoint=0xD7A3),  # Hangul syllables
    st.characters(min_codepoint=0x20, max_codepoint=0x7E),  # printable ASCII
    st.sampled_from([" ", "　", "\t"]),
)
subjects = st.one_of(
    st.text(chars, min_size=1, max_size=60),
    st.builds(lambda prefix, rest: prefix + rest, st.sampled_from(PREFIXES), st.text(chars, max_size=40)),
)
# (label written in the encoded-word, codec actually used) as real mailers do it
CHARSETS = [("utf-8", "utf-8"), ("UTF-8", "utf-8"), ("ks_c_5601-1987", "cp949"), ("euc-kr", "cp949")]


@st.composite
def encoded_subjects(draw):
    """A subject and one of many valid RFC 2047 encodings of it: split, folded, mixed charsets, B or Q."""
    text = draw(subjects)
    cuts = sorted(draw(st.sets(st.integers(1, len(text) - 1), max_size=3))) if len(text) > 1 else []
    words = []
    for piece in (text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])):
        label, codec = draw(st.sampled_from(CHARSETS))
        try:
            data = piece.encode(codec)
        except UnicodeEncodeError:
            label, data = "utf-8", piece.encode("utf-8")
        if draw(st.booleans()):
            words.append(f"=?{label}?B?{base64.b64encode(data).decode()}?=")
        else:
            words.append(quoprimime.header_encode(data, charset=label))
    separator = draw(st.sampled_from([" ", "\r\n ", "\r\n\t"]))
    return text, separator.join(words)


@given(encoded_subjects())
def test_any_valid_encoding_decodes_to_the_same_subject(case):
    text, header = case
    info = parse_headers(top_lines(message(header)))
    assert info.subject == text
    assert "undecodable subject" not in info.errors


@given(st.binary(max_size=200))
def test_arbitrary_subject_bytes_never_crash(raw):
    assert classify(parse_headers(top_lines(message(raw)))) in CATEGORIES


@given(st.text(alphabet="=?BbQq_AZaz09+/-* ", max_size=80))
def test_encoded_word_garbage_never_crashes(header):
    assert classify(parse_headers(top_lines(message(header)))) in CATEGORIES
