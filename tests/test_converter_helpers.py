from doc2agent.converter import safe_name


def test_safe_name_preserves_hebrew_and_unicode():
    assert safe_name("מסמך דרישות: API") == "מסמך דרישות_ API"


def test_safe_name_removes_windows_reserved_characters():
    assert safe_name('a/b:c*?"d<e>f|g') == "a_b_c___d_e_f_g"
