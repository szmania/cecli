from cecli.helpers.hashline import (
    HashlineError,
    hashline,
    parse_hashline,
    strip_hashline,
)


def test_hashline_basic():
    """Test basic hashline functionality."""
    text = "Hello\nWorld\nTest"
    result = hashline(text)

    # Check that we have 3 lines
    lines = result.splitlines()
    assert len(lines) == 3

    # Check each line has the format "[{4-char-hash}]content" (new HashPos format)
    for i, line in enumerate(lines, start=1):
        # Format should be "[{4-char-hash}]content"
        assert line.startswith("[")
        assert line[5] == "]"  # 4-char hash + 1 for opening bracket
        # Extract hash fragment
        hash_fragment = line[1:5]
        # Check hash fragment is 4 characters
        assert len(hash_fragment) == 4
        # Check all hash characters are valid base64 (A-Z, a-z, 0-9, -, _, @)
        for char in hash_fragment:
            assert char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_@"


def test_hashline_with_start_line():
    """Test hashline with custom start line."""
    text = "Line 1\nLine 2"
    result = hashline(text, start_line=10)

    lines = result.splitlines()
    assert len(lines) == 2
    # Check format is [{4-char-hash}]content (new HashPos format)
    # Note: start_line parameter is ignored by HashPos but kept for compatibility
    for line in lines:
        # Format should be "[{4-char-hash}]content"
        assert line.startswith("[")
        assert line[5] == "]"  # 4-char hash + 1 for opening bracket
        # Extract hash fragment
        hash_fragment = line[1:5]
        # Check hash fragment is 4 characters
        assert len(hash_fragment) == 4
        # Check all hash characters are valid base64 (A-Z, a-z, 0-9, -, _, @)
        for char in hash_fragment:
            assert char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_@"


def test_hashline_empty_string():
    """Test hashline with empty string."""
    result = hashline("")
    assert result == ""


def test_hashline_single_line():
    """Test hashline with single line."""
    text = "Single line"
    result = hashline(text)
    lines = result.splitlines()
    assert len(lines) == 1
    # Check format is [{4-char-hash}]content (new HashPos format)
    line = lines[0]
    assert line.startswith("[")
    assert line[5] == "]"  # 4-char hash + 1 for opening bracket
    assert line.endswith("]Single line")
    # Extract hash fragment
    hash_fragment = line[1:5]
    # Check hash fragment is 4 characters
    assert len(hash_fragment) == 4
    # Check all hash characters are valid base64 (A-Z, a-z, 0-9, -, _, @)
    for char in hash_fragment:
        assert char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_@"


def test_hashline_preserves_newlines():
    """Test that hashline preserves newline characters."""
    text = "Line 1\nLine 2\n"
    result = hashline(text)
    # HashPos format: [{4-char-hash}]content on each line
    # The result should have hashes on each line but no trailing newline
    lines = result.splitlines()
    assert len(lines) == 2
    # Check each line has the correct format
    for line in lines:
        assert line.startswith("[")
        assert line[5] == "]"  # 4-char hash + 1 for opening bracket
        # Extract hash fragment
        hash_fragment = line[1:5]
        assert len(hash_fragment) == 4
        # Check all hash characters are valid base64 (A-Z, a-z, 0-9, -, _, @)
        for char in hash_fragment:
            assert char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_@"
    # HashPos doesn't preserve trailing newlines in the formatted output
    # The splitlines() above verifies we have the right number of lines


def test_strip_hashline_basic():
    """Test basic strip_hashline functionality."""
    # Create a hashline-formatted text with correct HashPos format: [{4-char-hash}]content
    text = "[abcd]Hello\n[efgh]World\n[ijkl]Test"
    stripped = strip_hashline(text)
    assert stripped == "Hello\nWorld\nTest"


def test_strip_hashline_with_negative_line_numbers():
    """Test strip_hashline with negative line numbers."""
    # HashPos format doesn't support negative line numbers in the prefix
    # Test with standard HashPos format
    text = "[abcd]Hello\n[efgh]World\n[ijkl]Test"
    stripped = strip_hashline(text)
    assert stripped == "Hello\nWorld\nTest"


def test_strip_hashline_mixed_lines():
    """Test strip_hashline with mixed hashline and non-hashline lines."""
    # HashPos format: [{4-char-hash}]content
    # Plain lines without hashes should be left unchanged
    text = "[abcd]Hello\nPlain line\n[efgh]World"
    stripped = strip_hashline(text)
    assert stripped == "Hello\nPlain line\nWorld"


def test_strip_hashline_preserves_newlines():
    """Test that strip_hashline preserves newline characters."""
    # HashPos format: [{4-char-hash}]content
    text = "[abcd]Line 1\n[efgh]Line 2\n"
    stripped = strip_hashline(text)
    # strip_hashline should preserve newlines
    assert stripped == "Line 1\nLine 2\n"


def test_strip_hashline_empty_string():
    """Test strip_hashline with empty string."""
    assert strip_hashline("") == ""


def test_round_trip():
    """Test that strip_hashline can reverse hashline."""
    original = "Hello\nWorld\nTest\nMulti\nLine\nText"
    hashed = hashline(original)
    stripped = strip_hashline(hashed)
    assert stripped == original


def test_hashline_deterministic():
    """Test that hashline produces the same output for the same input."""
    text = "Hello World"
    result1 = hashline(text)
    result2 = hashline(text)
    assert result1 == result2


def test_hashline_different_inputs():
    """Test that different inputs produce different hashes."""
    text1 = "Hello"
    text2 = "World"
    result1 = hashline(text1)
    result2 = hashline(text2)

    # HashPos format: [{4-char-hash}]content
    # Extract hash from each line (there's only one line for single-line inputs)
    lines1 = result1.splitlines()
    lines2 = result2.splitlines()

    # Get the hash from each line (format: [hash]content)
    hash1 = lines1[0][1:5] if lines1 else ""  # Extract 4-char hash
    hash2 = lines2[0][1:5] if lines2 else ""  # Extract 4-char hash

    # Hashes should be different (very high probability)
    assert hash1 != hash2


def test_parse_hashline():
    """Test parse_hashline function."""
    # Test basic parsing (HashPos format: [{4-char-hash}])
    hash_fragment, line_num_str, line_num = parse_hashline("[abcd]")
    assert hash_fragment == "abcd"
    assert line_num_str is None  # HashPos doesn't include line numbers
    assert line_num is None

    # Test with content after hash
    hash_fragment, line_num_str, line_num = parse_hashline("[efgh]Hello World")
    assert hash_fragment == "efgh"
    assert line_num_str is None
    assert line_num is None

    # Test invalid format (should raise HashlineError)
    try:
        parse_hashline("invalid")
        assert False, "Expected HashlineError for invalid input"
    except HashlineError:
        pass  # Expected behavior
