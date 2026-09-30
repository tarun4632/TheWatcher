from app.area import in_area
from app.matcher import batch_years, min_cgpa, required_years, resolve_level


def test_india_and_remote_locations():
    assert in_area("Bengaluru, India")
    assert in_area("Remote")
    assert in_area("Remote (Worldwide)")
    assert in_area("")
    assert not in_area("Remote - US")
    assert not in_area("London, United Kingdom")


def test_years_batch_and_cgpa():
    assert required_years("We need 5+ years of experience in Python.") == 5
    assert required_years("No experience requirement stated.") is None
    assert 2025 in batch_years("Open to the 2025 batch")
    assert min_cgpa("Minimum CGPA of 7.5 is required") == 7.5
    assert min_cgpa("No grade cutoff") is None


def test_title_overrides_model_level():
    assert resolve_level("Software Intern", "experienced", 0.9, None) == "internship"
    assert resolve_level("Backend Engineer", "fresher", 0.9, 4) == "experienced"
