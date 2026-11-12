import pytest

from pipeline.helpers.website import website_domain


@pytest.mark.parametrize("website,expected", [
    ("https://www.nike.com/us/", "nike.com"),
    ("http://Example.co.uk", "example.co.uk"),
    ("nike.com/shop", "nike.com"),
    ("https://shop.brand.com:8080/x?y=1", "shop.brand.com"),
    ("", ""),
    (None, ""),
])
def test_website_domain(website, expected):
    assert website_domain(website) == expected
