"""
Tests for the participant links in ``settings.py`` (hai-public-access-spec FR-07, FR-08).

Each test maps to a settings row of the spec's section 9.2, or to the Python
side of AC-5 / AC-7. ``HAISettings.from_environment`` is given an explicit
mapping, so the developer's own environment does not leak in.
"""

from __future__ import annotations

import pytest

from settings import HAISettings

PUBLIC_HOST = "aieffect-dutch01.ewi.tudelft.nl"


def read_links(environment: dict[str, str]) -> tuple[str, str]:
    """Return (frontend_public_url, simulator_public_url) for an environment mapping."""
    settings = HAISettings.from_environment(environment)
    return settings.frontend_public_url, settings.simulator_public_url


def test_links_are_localhost_without_public_host():
    """AC-5: without HAI_PUBLIC_HOST the links stay on localhost:3200 / :5122."""
    # Given / When
    frontend_url, simulator_url = read_links({})

    # Then
    assert frontend_url == "http://localhost:3200"
    assert simulator_url == "http://localhost:5122"


def test_links_ignore_ports_without_public_host():
    """FR-02: public ports, even invalid ones, are unused while access is off."""
    # Given
    environment = {"HAI_FRONTEND_PUBLIC_PORT": "abc", "HAI_SIMULATOR_PUBLIC_PORT": "8443"}

    # When
    frontend_url, simulator_url = read_links(environment)

    # Then
    assert (frontend_url, simulator_url) == ("http://localhost:3200", "http://localhost:5122")


def test_links_use_default_public_ports_with_public_host():
    """FR-07: HAI_PUBLIC_HOST alone gives http://<host>:443 and http://<host>:8443."""
    # Given
    environment = {"HAI_PUBLIC_HOST": PUBLIC_HOST}

    # When
    frontend_url, simulator_url = read_links(environment)

    # Then
    assert frontend_url == f"http://{PUBLIC_HOST}:443"
    assert simulator_url == f"http://{PUBLIC_HOST}:8443"


def test_links_use_custom_public_ports():
    """FR-07: configured public ports replace the defaults."""
    # Given
    environment = {
        "HAI_PUBLIC_HOST": "h",
        "HAI_FRONTEND_PUBLIC_PORT": "8080",
        "HAI_SIMULATOR_PUBLIC_PORT": " 9090 ",
    }

    # When
    frontend_url, simulator_url = read_links(environment)

    # Then
    assert (frontend_url, simulator_url) == ("http://h:8080", "http://h:9090")


def test_explicit_url_wins_over_public_host():
    """FR-07: an explicit HAI_FRONTEND_PUBLIC_URL wins; the simulator link is still derived."""
    # Given
    environment = {"HAI_PUBLIC_HOST": "h", "HAI_FRONTEND_PUBLIC_URL": "http://x"}

    # When
    frontend_url, simulator_url = read_links(environment)

    # Then
    assert (frontend_url, simulator_url) == ("http://x", "http://h:8443")


def test_blank_values_count_as_unset():
    """FR-02: a blank host, and the blank URLs docker-compose.yml passes, mean "not set"."""
    # Given
    environment = {
        "HAI_PUBLIC_HOST": "   ",
        "HAI_FRONTEND_PUBLIC_URL": "",
        "HAI_SIMULATOR_PUBLIC_URL": "",
        "HAI_FRONTEND_PUBLIC_PORT": "",
    }

    # When
    frontend_url, simulator_url = read_links(environment)

    # Then
    assert (frontend_url, simulator_url) == ("http://localhost:3200", "http://localhost:5122")


@pytest.mark.parametrize(
    ("port_variable", "invalid_value"),
    [
        ("HAI_FRONTEND_PUBLIC_PORT", "abc"),
        ("HAI_FRONTEND_PUBLIC_PORT", "70000"),
        ("HAI_SIMULATOR_PUBLIC_PORT", "0"),
        ("HAI_SIMULATOR_PUBLIC_PORT", "-1"),
        ("HAI_SIMULATOR_PUBLIC_PORT", "²"),
    ],
)
def test_invalid_public_port_raises_value_error(port_variable: str, invalid_value: str):
    """AC-7: with HAI_PUBLIC_HOST set, an invalid port raises ValueError naming the variable."""
    # Given
    environment = {"HAI_PUBLIC_HOST": "h", port_variable: invalid_value}

    # When / Then
    with pytest.raises(ValueError, match=port_variable):
        HAISettings.from_environment(environment)
