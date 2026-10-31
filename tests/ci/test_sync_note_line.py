from scripts.ci.sync_note_line import is_note_line


def test_blank_is_a_note() -> None:
    assert is_note_line("  \n", language="other") is True


def test_js_line_comment_is_a_note() -> None:
    assert is_note_line("  // kept\n", language="other") is True


def test_hash_comment_is_a_note() -> None:
    assert is_note_line("# kept\n", language="other") is True


def test_preprocessor_stops() -> None:
    assert is_note_line("#include <x>\n", language="other") is False


def test_code_after_comment_stops() -> None:
    assert is_note_line("x = 1  # note\n", language="other") is False


def test_js_block_comment_stops() -> None:
    assert is_note_line("/* hidden */\n", language="other") is False


def test_template_line_that_starts_with_code_stops() -> None:
    assert is_note_line("const x = 1\n", language="other") is False


def test_python_comment_is_a_note() -> None:
    source = "x = 1\n# kept\n"
    assert is_note_line("# kept\n", language="python", source=source, line_number=2) is True


def test_python_docstring_line_is_a_note() -> None:
    source = 'def f():\n    """kept"""\n    return 1\n'
    assert is_note_line('    """kept"""\n', language="python", source=source, line_number=2) is True


def test_python_string_value_stops() -> None:
    source = 'x = """kept"""\n'
    assert is_note_line('x = """kept"""\n', language="python", source=source, line_number=1) is False


def test_python_string_after_a_statement_stops() -> None:
    source = 'x = 1\n"""not a docstring"""\n'
    assert is_note_line('"""not a docstring"""\n', language="python", source=source, line_number=2) is False


def test_tokenize_failure_stops() -> None:
    assert is_note_line("# kept\n", language="python", source="def (\n", line_number=1) is False
