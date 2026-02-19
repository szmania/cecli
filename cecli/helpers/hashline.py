import difflib
import re

import xxhash


class HashlineError(Exception):
    """Custom exception for hashline-specific errors."""

    pass


def hashline(text: str, start_line: int = 1) -> str:
    """
    Add a hash scheme to each line of text.

    For each line in the input text, returns a string where each line is prefixed with:
    "{2-digit base52 of xxhash mod 52^2}:{line number}|{line contents}"

    Args:
        text: Input text (most likely representing a file's text)
        start_line: Starting line number (default: 1)

    Returns:
        String with hash scheme added to each line
    """
    lines = text.splitlines(keepends=True)
    result_lines = []

    for i, line in enumerate(lines, start=start_line):
        # Calculate xxhash for the line content
        hash_value = xxhash.xxh3_64_intdigest(line.encode("utf-8"))

        # Use mod 52^2 (2704) for faster computation
        mod_value = hash_value % 2704  # 52^2 = 2704

        # Convert to 2-digit base52 using helper function
        last_two_str = int_to_2digit_52(mod_value)

        # Format the line
        formatted_line = f"{last_two_str}:{i}|{line}"
        result_lines.append(formatted_line)

    return "".join(result_lines)


def int_to_2digit_52(n: int) -> str:
    """
    Convert integer to 2-digit base52 with 'a' padding.

    Base52 uses characters: a-z (lowercase) and A-Z (uppercase).

    Args:
        n: Integer in range 0-2703 (52^2 - 1)

    Returns:
        2-character base52 string
    """
    # Ensure n is in valid range
    n = n % 2704  # 52^2

    # Convert to base52
    if n == 0:
        return "aa"

    digits = []
    while n > 0:
        n, remainder = divmod(n, 52)
        if remainder < 26:
            # a-z (lowercase)
            digits.append(chr(remainder + ord("a")))
        else:
            # A-Z (uppercase)
            digits.append(chr(remainder - 26 + ord("A")))

    # Pad to 2 digits with 'a'
    while len(digits) < 2:
        digits.append("a")

    # Return in correct order (most significant first)
    return "".join(reversed(digits))


def strip_hashline(text: str) -> str:
    """
    Remove hashline-like sequences from the start of every line.

    Removes prefixes that match the pattern: "{2-digit base52}:{line number}|"
    where line number can be any integer (positive, negative, or zero) and
    the 2-digit base52 is exactly 2 characters from the set [a-zA-Z].

    Args:
        text: Input text with hashline prefixes

    Returns:
        String with hashline prefixes removed from each line
    """
    import re

    # Pattern to match: {2 base52 chars}:{optional minus sign}{digits}|
    # The hash is exactly 2 characters from [a-zA-Z]
    # The line number can be any integer (positive, negative, or zero)
    pattern = r"^[a-zA-Z]{2}:-?\d+\|"

    lines = text.splitlines(keepends=True)
    result_lines = []

    for line in lines:
        # Remove the hashline prefix if present
        stripped_line = re.sub(pattern, "", line, count=1)
        result_lines.append(stripped_line)

    return "".join(result_lines)


def parse_hashline(hashline_str: str):
    """
    Parse a hashline string into hash fragment and line number.

    Args:
        hashline_str: Hashline format string: "{hash_fragment}:{line_num}"

    Returns:
        tuple: (hash_fragment, line_num_str, line_num)

    Raises:
        HashlineError: If format is invalid
    """
    if hashline_str is None:
        raise HashlineError("Hashline string cannot be None")

    try:
        hashline_str = hashline_str.rstrip("|")
        hash_fragment, line_num_str = hashline_str.split(":", 1)
        line_num = int(line_num_str)
        return hash_fragment, line_num_str, line_num
    except ValueError as e:
        raise HashlineError(f"Invalid hashline format '{hashline_str}': {e}")


def normalize_hashline(hashline_str: str) -> str:
    """
    Normalize a hashline string to the proper "{hash_fragment}:{line_num}" format.

    Accepts hashline strings in either "{hash_fragment}:{line_num}" format or
    "{line_num}:{hash_fragment}" format and returns it in the proper format.

    Args:
        hashline_str: Hashline string in either format

    Returns:
        str: Hashline string in "{hash_fragment}:{line_num}" format

    Raises:
        HashlineError: If format is invalid
    """
    if hashline_str is None:
        raise HashlineError("Hashline string cannot be None")

    # Remove trailing pipe if present
    hashline_str = hashline_str.rstrip("|")

    # Try to parse as "{hash_fragment}:{line_num}" first
    pattern1 = r"^([a-zA-Z]{2}):(\d+)$"
    match1 = re.match(pattern1, hashline_str)
    if match1:
        # Already in correct format
        return hashline_str

    # Try to parse as "{line_num}:{hash_fragment}"
    pattern2 = r"^(\d+):([a-zA-Z]{2})$"
    match2 = re.match(pattern2, hashline_str)
    if match2:
        line_num_str, hash_fragment = match2.groups()
        return f"{hash_fragment}:{line_num_str}"

    # If neither pattern matches, raise error
    raise HashlineError(
        f"Invalid hashline format '{hashline_str}'. "
        "Expected either '{hash_fragment}:{line_num}' or '{line_num}:{hash_fragment}' "
        "where hash_fragment is exactly 2 letters and line_num is a positive integer."
    )


def find_hashline_by_exact_match(hashed_lines, hash_fragment, line_num_str):
    """
    Find a hashline by exact hash_fragment:line_num match.

    Args:
        hashed_lines: List of hashed lines
        hash_fragment: Hash fragment to match
        line_num_str: Line number as string

    Returns:
        int: Index of matching line, or None if not found
    """
    for i, line in enumerate(hashed_lines):
        if line.startswith(f"{hash_fragment}:{line_num_str}|"):
            return i
    return None


def find_hashline_by_fragment(hashed_lines, hash_fragment, target_line_num=None):
    """
    Find a hashline by hash fragment only.

    Args:
        hashed_lines: List of hashed lines
        hash_fragment: Hash fragment to search for
        target_line_num: Optional target line number to find closest match

    Returns:
        int: Index of line with matching hash fragment, or None if not found.
             If target_line_num is provided, returns the match with smallest
             absolute distance to target_line_num.
    """
    matches = []
    for i, line in enumerate(hashed_lines):
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        line_hash_fragment = parts[0]
        if line_hash_fragment == hash_fragment:
            if target_line_num is None:
                return i  # Return first match for backward compatibility

            # Extract line number from hashline
            line_num_part = parts[1].split("|", 1)[0]
            try:
                line_num = int(line_num_part)
                distance = abs(line_num - target_line_num)
                matches.append((distance, i, line_num))
            except ValueError:
                # If line number can't be parsed, treat as distance 0
                matches.append((0, i, 0))

    if not matches:
        return None

    if target_line_num is None:
        # Should not reach here if target_line_num is None (returned above)
        return matches[0][1] if matches else None

    # Return the match with smallest distance
    matches.sort(key=lambda x: x[0])
    return matches[0][1]


def find_hashline_range(
    hashed_lines,
    start_line_hash,
    end_line_hash,
    allow_exact_match=True,
):
    """
    Find start and end line indices in hashed content.

    Args:
        hashed_lines: List of hashed lines
        start_line_hash: Hashline format for start line
        end_line_hash: Hashline format for end line
        allow_exact_match: Whether to try exact match first (default: True)

    Returns:
        tuple: (found_start_line, found_end_line)

    Raises:
        HashlineError: If range cannot be found or is invalid
    """
    # Parse start_line_hash
    start_hash_fragment, start_line_num_str, start_line_num = parse_hashline(start_line_hash)

    # Try to find start line
    found_start_line = None
    if allow_exact_match:
        found_start_line = find_hashline_by_exact_match(
            hashed_lines, start_hash_fragment, start_line_num_str
        )

    if found_start_line is None:
        found_start_line = find_hashline_by_fragment(
            hashed_lines, start_hash_fragment, start_line_num
        )

    if found_start_line is None:
        raise HashlineError(f"Start line hash fragment '{start_hash_fragment}' not found in file")

    # Parse end_line_hash
    end_hash_fragment, end_line_num_str, end_line_num = parse_hashline(end_line_hash)

    # Try to find end line
    found_end_line = None
    if allow_exact_match:
        found_end_line = find_hashline_by_exact_match(
            hashed_lines, end_hash_fragment, end_line_num_str
        )

    if found_end_line is None:
        # Calculate line distance
        line_distance = end_line_num - start_line_num
        if line_distance < 0:
            raise HashlineError(
                f"End line {end_line_num} must be equal to or after start line {start_line_num}"
            )

        # Check if end hash fragment exists at the expected distance
        expected_found_end_line = found_start_line + line_distance
        if expected_found_end_line >= len(hashed_lines):
            raise HashlineError(
                f"Start hash fragment found at line {found_start_line + 1}, but "
                f"end line {expected_found_end_line + 1} is out of range."
            )

        # Check if end hash fragment matches at the expected position
        # If not, use find_hashline_by_fragment() to find the closest match
        actual_end_hashed_line = hashed_lines[expected_found_end_line]
        actual_end_hash_fragment = actual_end_hashed_line.split(":", 1)[0]

        if actual_end_hash_fragment != end_hash_fragment:
            # Instead of raising an error, try to find the closest matching hash fragment
            # near where the end line would be based on distance from start line
            found_end_line = find_hashline_by_fragment(
                hashed_lines, end_hash_fragment, expected_found_end_line
            )
            if found_end_line is None:
                raise HashlineError(
                    f"End line hash fragment '{end_hash_fragment}' not found near "
                    f"expected position {expected_found_end_line + 1}."
                )
        else:
            found_end_line = expected_found_end_line

    # Verify end line is not before start line
    if found_end_line < found_start_line:
        raise HashlineError(
            f"End line {found_end_line + 1} must be equal to or after start line"
            f" {found_start_line + 1}"
        )

    return found_start_line, found_end_line


def apply_hashline_operation(
    original_content,
    start_line_hash,
    end_line_hash=None,
    operation="replace",
    text=None,
):
    """
    Apply an operation (replace, insert, delete) using hashline ranges.

    Uses regex/find to locate hashline ranges in the content and applies
    the specified operation directly.

    Note: Ranges are inclusive of both start and end boundaries.
    For example, a range from line 3 to line 6 includes lines 3, 4, 5, and 6.

    Args:
        original_content: Original file content
        start_line_hash: Hashline format for start line: "{hash_fragment}:{line_num}"
        end_line_hash: Hashline format for end line: "{hash_fragment}:{line_num}" (optional for insert operations)
        operation: One of "replace", "insert", or "delete"
        text: Text to insert or replace with (required for replace/insert operations)

    Returns:
        Modified content after applying the operation

    Raises:
        HashlineError: If hashline verification fails or operation is invalid
    """
    # Normalize hashline inputs
    start_line_hash = normalize_hashline(start_line_hash)
    if end_line_hash is not None:
        end_line_hash = normalize_hashline(end_line_hash)

    # Handle empty content as a special case
    if original_content == "" or original_content is None:
        if operation == "insert" or operation == "replace":
            if text is None:
                raise HashlineError(
                    f"Text parameter is required for '{operation}' operation on empty file"
                )
            # For empty files, just return the text to insert/replace with
            return text if text.endswith("\n") else text + "\n"
        elif operation == "delete":
            # Deleting from empty file returns empty
            return ""
        else:
            # Should not happen due to validation above, but handle anyway
            raise HashlineError(f"Invalid operation '{operation}' for empty file")

    # Validate operation
    valid_operations = {"replace", "insert", "delete"}
    if operation not in valid_operations:
        raise HashlineError(
            f"Invalid operation '{operation}'. Must be one of: {', '.join(valid_operations)}"
        )

    # Validate text parameter for replace/insert operations
    if operation in {"replace", "insert"} and text is None:
        raise HashlineError(f"Text parameter is required for '{operation}' operation")

    # Apply hashline to original content
    hashed_original = hashline(original_content)
    hashed_lines = hashed_original.splitlines(keepends=True)

    # Parse start_line_hash to get hash fragment for insert operations
    start_hash_fragment, start_line_num_str, start_line_num = parse_hashline(start_line_hash)

    # Special case: 0:00 means insert at the beginning of the file for both insert and replace operations
    if start_line_num == 0 and operation in {"insert", "replace"}:
        found_start_line = -1  # Insert before line 0 (i.e., at the beginning)
    elif operation == "insert":
        # Try exact match first, then fragment match
        found_start_line = find_hashline_by_exact_match(
            hashed_lines, start_hash_fragment, start_line_num_str
        )
        if found_start_line is None:
            found_start_line = find_hashline_by_fragment(
                hashed_lines, start_hash_fragment, start_line_num
            )

        if found_start_line is None:
            raise HashlineError(
                f"Start line hash fragment '{start_hash_fragment}' not found in file"
            )

    # Handle insert operations and replace operations with 0:00
    if operation == "insert" or (operation == "replace" and start_line_num == 0):
        # For insert operations, we only need the start line
        # Insert after the start line
        text_to_insert = text
        if text_to_insert and not text_to_insert.endswith("\n"):
            text_to_insert += "\n"

        # Check if the line we're inserting after ends with a newline
        # If it doesn't, we need to add a newline to separate the inserted text
        target_line = hashed_lines[found_start_line]
        if not target_line.endswith("\n"):
            # Add a newline to the target line before inserting
            hashed_lines[found_start_line] = target_line + "\n"

        # Insert the text after the start line
        new_hashed_lines = (
            hashed_lines[: found_start_line + 1]
            + [text_to_insert]
            + hashed_lines[found_start_line + 1 :]
        )

        # Reconstruct hashed content
        new_hashed_content = "".join(new_hashed_lines)

        # Strip hashline prefixes to get back to original format
        new_content = strip_hashline(new_hashed_content)

        return new_content

    # For replace and delete operations, use find_hashline_range
    found_start_line, found_end_line = find_hashline_range(
        hashed_lines,
        start_line_hash,
        end_line_hash,
        allow_exact_match=True,
    )

    # Now we have the exact range in the hashed content
    # Apply the operation based on the type

    if operation == "delete":
        # Delete the range from start to end (inclusive)
        new_hashed_lines = hashed_lines[:found_start_line] + hashed_lines[found_end_line + 1 :]

    elif operation == "replace":
        # Replace the range with new text
        # First, apply hashline to the replacement text
        replacement_text = text
        if not replacement_text.endswith("\n"):
            replacement_text += "\n"

        # Replace the range
        new_hashed_lines = (
            hashed_lines[:found_start_line]
            + [replacement_text]
            + hashed_lines[found_end_line + 1 :]
        )
    else:
        # This should not happen since we already handled insert operations
        raise HashlineError(f"Unexpected operation '{operation}' in replace/delete section")

    # Reconstruct hashed content
    new_hashed_content = "".join(new_hashed_lines)

    # Strip hashline prefixes to get back to original format
    new_content = strip_hashline(new_hashed_content)

    return new_content


def extract_hashline_range(
    original_content,
    start_line_hash,
    end_line_hash,
):
    """
    Extract the content between hashline markers.

    Args:
        original_content: Original file content
        start_line_hash: Hashline format for start line: "{hash_fragment}:{line_num}"
        end_line_hash: Hashline format for end line: "{hash_fragment}:{line_num}"

    Returns:
        str: The extracted content between the hashline markers (with hashline prefixes preserved)

    Raises:
        HashlineError: If hashline verification fails
    """
    # Normalize hashline inputs
    start_line_hash = normalize_hashline(start_line_hash)
    end_line_hash = normalize_hashline(end_line_hash)

    # Apply hashline to original content to find the range
    hashed_original = hashline(original_content)
    hashed_lines = hashed_original.splitlines(keepends=True)

    # Use find_hashline_range to locate the range
    found_start_line, found_end_line = find_hashline_range(
        hashed_lines,
        start_line_hash,
        end_line_hash,
        allow_exact_match=True,
    )

    # Now we have the exact range in the hashed content
    # Extract the original content from the range
    original_range_lines = hashed_lines[found_start_line : found_end_line + 1]
    original_range_content = "".join(original_range_lines)

    # Return the hashed content (with hashline prefixes preserved)
    return original_range_content


def find_best_line(content, target_line_num, content_to_lines, used_lines, hashlines):
    """
    Find the best matching line for given content near target_line_num.

    This helper function is used by get_hashline_content_diff to handle duplicate lines.
    It finds the line number closest to the target position that hasn't been used yet.

    Args:
        content: The content to find
        target_line_num: The target line number we're trying to match
        content_to_lines: Dictionary mapping content to list of line numbers where it appears
        used_lines: Set of line numbers that have already been used
        hashlines: List of hashline-prefixed lines

    Returns:
        tuple: (best_line_num, best_hashline) or None if not found
    """
    if content not in content_to_lines:
        return None

    # Get all line numbers where this content appears
    line_numbers = content_to_lines[content]

    # Filter out already used lines
    available_lines = [ln for ln in line_numbers if ln not in used_lines]

    if not available_lines:
        return None

    # Find the line closest to the target line number
    # For diffs, we want the line that's in the right position
    best_line_num = min(available_lines, key=lambda ln: abs(ln - target_line_num))
    return best_line_num, hashlines[best_line_num - 1]  # Convert to 0-based index


def get_hashline_diff(
    original_content,
    start_line_hash,
    end_line_hash,
    operation,
    text=None,
):
    """
    Generate a diff for a hashline operation in the format used by the original format_output.
    Returns a diff between the original range content and the replacement text.

    Args:
        original_content: Original file content
        start_line_hash: Hashline format for start line: "{hash_fragment}:{line_num}"
        end_line_hash: Hashline format for end line: "{hash_fragment}:{line_num}"
        operation: One of "replace", "insert", or "delete"
        text: Text to insert or replace with (required for replace/insert operations)

    Returns:
        str: A formatted diff snippet showing changes, or empty string if no changes

    Raises:
        HashlineError: If hashline verification fails or operation is invalid
    """

    if operation == "insert":
        end_line_hash = start_line_hash

    # Extract the original range content using the new helper method
    # This now returns the hashed content with hashlines preserved
    original_range_content = extract_hashline_range(
        original_content=original_content,
        start_line_hash=start_line_hash,
        end_line_hash=end_line_hash,
    )

    # Parse start_line_hash to get the start line number
    try:
        _, start_line_num_str, start_line_num = parse_hashline(start_line_hash)
    except ValueError as e:
        raise HashlineError(f"Invalid start_line_hash format '{start_line_hash}': {e}")

    # For delete operation, we're removing the range
    if operation == "delete":
        find_text = original_range_content
        replace_text = ""
    # For insert operation, we're inserting after the range
    elif operation == "insert":
        find_text = ""
        # For insert operations, we need to calculate hashlines for the text to insert
        # The text should be hashed starting at the line after the end line
        if text:
            # Parse end_line_hash to get the end line number
            try:
                _, end_line_num_str, end_line_num = parse_hashline(end_line_hash)
            except ValueError as e:
                raise HashlineError(f"Invalid end_line_hash format '{end_line_hash}': {e}")
            # Insert after the end line, so start hashline at end_line_num + 1
            replace_text = hashline(text, start_line=end_line_num + 1)
        else:
            replace_text = ""
    # For replace operation, we're replacing the range
    elif operation == "replace":
        find_text = original_range_content
        # For replace operations, the replacement text should be hashed starting at the start line
        if text:
            replace_text = hashline(text, start_line=start_line_num)
        else:
            replace_text = ""
    else:
        raise HashlineError(
            f"Invalid operation '{operation}'. Must be one of: replace, insert, delete"
        )

    # Generate diff in the same format as original format_output
    # Use splitlines(keepends=True) to preserve line endings for accurate hash comparison
    find_lines = find_text.splitlines(keepends=True)
    replace_lines = replace_text.splitlines(keepends=True)

    # Strip line endings for difflib comparison but keep them in the actual lines
    diff = difflib.unified_diff(
        [line.rstrip("\r\n") for line in find_lines],
        [line.rstrip("\r\n") for line in replace_lines],
        lineterm="",
        n=1,
    )

    # Skip header lines (first 2 lines) as in original format_output
    diff_lines = list(diff)[2:]

    if diff_lines:
        return "\n".join([line for line in diff_lines])
    else:
        return ""


CHUNK_HEADER_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_content_for_diff(content: str):
    """Helper to parse hashline content in a single pass."""
    hashlines = []
    content_to_lines = {}
    content_only_lines = []

    for line_num, line in enumerate(content.splitlines(keepends=True), 1):
        if "|" in line:
            parts = line.split("|", 1)
            if len(parts) == 2:
                line_content = parts[1].rstrip("\r\n")
                hashline_prefixed = line.rstrip("\r\n")
                hashlines.append(hashline_prefixed)
                content_only_lines.append(line_content)
                if line_content not in content_to_lines:
                    content_to_lines[line_content] = []
                content_to_lines[line_content].append(line_num)
                continue

        # Line without hashline prefix or malformed
        stripped = line.rstrip("\r\n")
        hashlines.append(stripped)
        content_only_lines.append(stripped)
        if stripped not in content_to_lines:
            content_to_lines[stripped] = []
        content_to_lines[stripped].append(line_num)

    return hashlines, content_to_lines, content_only_lines


def get_hashline_content_diff(
    old_content: str, new_content: str, fromfile: str = "", tofile: str = "", context_lines: int = 1
) -> str:
    """
    Generate a unified diff between two hashline-prefixed contents.

    This function generates a content-only diff first, then uses it as a template
    to build a hashline diff that only shows actual content changes.

    Args:
        old_content: Old content with hashline prefixes
        new_content: New content with hashline prefixes
        fromfile: Optional filename for the old content in diff header
        tofile: Optional filename for the new content in diff header

    Returns:
        str: Unified diff string, or empty string if no changes
    """
    if old_content == new_content:
        return ""

    # Generate content-only versions by stripping hashline prefixes
    # Parse the original hashline content into lists for lookup
    old_hashlines, old_content_to_lines, old_content_lines = _parse_content_for_diff(old_content)
    new_hashlines, new_content_to_lines, new_content_lines = _parse_content_for_diff(new_content)

    # Generate content-only diff
    content_diff = difflib.unified_diff(
        old_content_lines,
        new_content_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
        n=context_lines,
    )
    content_diff_lines = list(content_diff)

    # If there's no content change, return empty string
    if not content_diff_lines:
        return ""

    # Build the hashline diff using the content diff as a template
    # We need to track which lines have been "used" to handle duplicates
    old_used_lines = set()
    new_used_lines = set()
    hashline_diff_lines = []

    # Parse the content diff to understand line numbers
    current_old_line = 1
    current_new_line = 1

    for line in content_diff_lines:
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            # Keep headers and chunk headers as-is
            hashline_diff_lines.append(line)

            # Parse chunk header to update line numbers
            if line.startswith("@@"):
                match = CHUNK_HEADER_RE.match(line)
                if match:
                    current_old_line = int(match.group(1))
                    current_new_line = int(match.group(3))
        elif line.startswith(" ") or line.startswith("-") or line.startswith("+"):
            # This is a content line
            marker = line[0]
            content = line[1:]

            if marker == " ":
                # Context line - exists in both
                # Try to find matching line in new content first
                result = find_best_line(
                    content, current_new_line, new_content_to_lines, new_used_lines, new_hashlines
                )
                if result:
                    best_line_num, best_hashline = result
                    hashline_diff_lines.append(f" {best_hashline}")
                    # Mark this line as used
                    new_used_lines.add(best_line_num)
                else:
                    # Fallback to old content
                    result = find_best_line(
                        content,
                        current_old_line,
                        old_content_to_lines,
                        old_used_lines,
                        old_hashlines,
                    )
                    if result:
                        best_line_num, best_hashline = result
                        hashline_diff_lines.append(f" {best_hashline}")
                        # Mark this line as used
                        old_used_lines.add(best_line_num)
                    else:
                        # Fallback: use the content as-is
                        hashline_diff_lines.append(line)

                current_old_line += 1
                current_new_line += 1

            elif marker == "-":
                # Line removed - exists in old
                result = find_best_line(
                    content, current_old_line, old_content_to_lines, old_used_lines, old_hashlines
                )
                if result:
                    best_line_num, best_hashline = result
                    hashline_diff_lines.append(f"-{best_hashline}")
                    # Mark this line as used
                    old_used_lines.add(best_line_num)
                else:
                    # Fallback: use the content as-is
                    hashline_diff_lines.append(line)

                current_old_line += 1

            elif marker == "+":
                # Line added - exists in new
                result = find_best_line(
                    content, current_new_line, new_content_to_lines, new_used_lines, new_hashlines
                )
                if result:
                    best_line_num, best_hashline = result
                    hashline_diff_lines.append(f"+{best_hashline}")
                    # Mark this line as used
                    new_used_lines.add(best_line_num)
                else:
                    # Fallback: use the content as-is
                    hashline_diff_lines.append(line)

                current_new_line += 1
        else:
            # Unknown line type, keep as-is
            hashline_diff_lines.append(line)

    diff_text = "\n".join(hashline_diff_lines)
    return diff_text if diff_text.strip() else ""


def apply_hashline_operations(
    original_content: str,
    operations: list,
) -> tuple[str, list, list]:
    """
    Apply multiple hashline operations sequentially.

    This function hashes the content once, resolves all operations to line indices,
    and applies them in reverse order (bottom-to-top) to avoid line number shifts.

    Args:
        original_content: Original file content
        operations: List of operation dictionaries

    Returns:
        tuple: (modified_content, successful_operations, failed_operations)
        - modified_content: Modified content after applying all operations
        - successful_operations: List of successfully applied operation indices
        - failed_operations: List of dictionaries with failed operation info
          Each dict contains: {"index": int, "error": str, "operation": dict}
    """
    # Normalize hashline inputs in operations
    normalized_operations = []
    failed_ops = []
    for i, op in enumerate(operations):
        try:
            normalized_op = op.copy()
            normalized_op["start_line_hash"] = normalize_hashline(op["start_line_hash"])
            if "end_line_hash" in op:
                normalized_op["end_line_hash"] = normalize_hashline(op["end_line_hash"])
            normalized_operations.append(normalized_op)
        except Exception as e:
            failed_ops.append({"index": i, "error": str(e), "operation": op})

    if not normalized_operations:
        return original_content, [], failed_ops

    # Apply hashline to original content once
    hashed_content = hashline(original_content)
    hashed_lines = hashed_content.splitlines(keepends=True)

    # Resolve all operations to indices first
    resolved_ops = []
    for i, op in enumerate(normalized_operations):
        try:
            if op["operation"] == "insert":
                start_hash_fragment, start_line_num_str, start_line_num = parse_hashline(
                    op["start_line_hash"]
                )

                # Try exact match first for insert operations
                found_start = find_hashline_by_exact_match(
                    hashed_lines, start_hash_fragment, start_line_num_str
                )

                if found_start is None:
                    found_start = find_hashline_by_fragment(
                        hashed_lines, start_hash_fragment, start_line_num
                    )

                if found_start is None:
                    raise HashlineError(
                        f"Start line hash fragment '{start_hash_fragment}' not found in file"
                    )

                resolved_ops.append(
                    {"index": i, "start_idx": found_start, "end_idx": found_start, "op": op}
                )
            else:
                # Use find_hashline_range for replace/delete to leverage its robust logic
                # which handles exact matches (including line numbers) and relative offsets
                found_start, found_end = find_hashline_range(
                    hashed_lines, op["start_line_hash"], op["end_line_hash"], allow_exact_match=True
                )

                resolved_ops.append(
                    {"index": i, "start_idx": found_start, "end_idx": found_end, "op": op}
                )
        except Exception as e:
            failed_ops.append({"index": i, "error": str(e), "operation": op})

    # Optimize: discard inner ranges that are completely contained within outer ranges
    # This prevents redundant operations and potential errors
    optimized_ops = []
    for i, op_a in enumerate(resolved_ops):
        keep_op = True

        # Check if this operation is contained within any other operation
        for j, op_b in enumerate(resolved_ops):
            if i == j:
                continue

            # Check if op_a is completely inside op_b
            # op_a is inside op_b if:
            # op_b.start_idx <= op_a.start_idx and op_a.end_idx <= op_b.end_idx
            if op_b["start_idx"] <= op_a["start_idx"] and op_a["end_idx"] <= op_b["end_idx"]:
                # Special case: operations with the same indices but different types
                # should both be kept (e.g., replace and insert at same line)
                if (
                    op_a["start_idx"] == op_b["start_idx"]
                    and op_a["end_idx"] == op_b["end_idx"]
                    and op_a["op"]["operation"] != op_b["op"]["operation"]
                ):
                    # Keep both operations if they have different types
                    continue
                # op_a is inside op_b, discard op_a
                keep_op = False
                break

        if keep_op:
            optimized_ops.append(op_a)

    # Replace resolved_ops with optimized version
    resolved_ops = optimized_ops

    # Sort by start_idx descending to apply from bottom to top
    # When operations have same start_idx, apply in order: insert, replace, delete
    # This ensures correct behavior when multiple operations target the same line
    def sort_key(op):
        start_idx = op["start_idx"]
        # Operation type priority: insert (0), replace (1), delete (2)
        # Lower priority number means applied first
        op_type = op["op"]["operation"]
        if op_type == "insert":
            priority = 0
        elif op_type == "replace":
            priority = 1
        else:  # delete
            priority = 2
        # Sort by start_idx descending, then priority ascending
        return (-start_idx, priority)

    resolved_ops.sort(key=sort_key)

    successful_ops = []
    for resolved in resolved_ops:
        try:
            op = resolved["op"]
            start_idx = resolved["start_idx"]
            end_idx = resolved["end_idx"]

            if op["operation"] == "insert":
                text = op["text"]
                if text and not text.endswith("\n"):
                    text += "\n"
                if not hashed_lines[start_idx].endswith("\n"):
                    hashed_lines[start_idx] += "\n"
                hashed_lines.insert(start_idx + 1, text)
            elif op["operation"] == "delete":
                del hashed_lines[start_idx : end_idx + 1]
            elif op["operation"] == "replace":
                text = op["text"]
                if text:
                    # Split text into lines, preserving trailing newline behavior
                    # If text doesn't end with newline, we add one to ensure proper line separation
                    if not text.endswith("\n"):
                        text += "\n"
                    # Split into lines and replace the range
                    replacement_lines = text.splitlines(keepends=True)
                    hashed_lines[start_idx : end_idx + 1] = replacement_lines
                else:
                    # Empty text - replace with nothing (delete)
                    hashed_lines[start_idx : end_idx + 1] = []

            successful_ops.append(resolved["index"])
        except Exception as e:
            failed_ops.append(
                {"index": resolved["index"], "error": str(e), "operation": resolved["op"]}
            )

    # Join and strip hashlines
    result_with_hashes = "".join(hashed_lines)
    result = strip_hashline(result_with_hashes)

    # Respect original trailing newline
    if not original_content.endswith("\n") and result.endswith("\n"):
        result = result[:-1]

    return result, successful_ops, failed_ops
