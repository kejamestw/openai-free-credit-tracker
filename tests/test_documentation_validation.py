from scripts.validate_docs import validate_documentation


def test_documentation_links_and_bilingual_coverage_are_valid():
    assert validate_documentation() == []
