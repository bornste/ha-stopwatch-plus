"""Tests for stopwatches that follow several source entities, and the migration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.stopwatch_plus.const import (
    CONF_AUTO_STOP,
    CONF_AUTO_STOP_DELAY,
    CONF_GRACE_PERIOD,
    CONF_INTERVAL,
    CONF_RUNNING_STATES,
    CONF_SOURCE_COMBINE,
    CONF_SOURCE_ENTITIES,
    CONF_SOURCE_ENTITY,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.stopwatch_plus.source import SourceState, combine_states
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant

from .conftest import ELAPSED, PAUSE, STATUS

XBOX = "media_player.xbox"
PLAYSTATION = "media_player.playstation"
IN_GAME = "binary_sensor.xbox_in_game"

RUNNING = SourceState.RUNNING
INACTIVE = SourceState.INACTIVE
UNAVAILABLE = SourceState.UNAVAILABLE


def _options(entities: list[str], combine: str, **extra: Any) -> dict[str, Any]:
    """Options of a stopwatch following the given entities."""
    return {
        CONF_INTERVAL: 0,
        CONF_UPDATE_INTERVAL: 60,
        CONF_SOURCE_ENTITIES: entities,
        CONF_SOURCE_COMBINE: combine,
        CONF_RUNNING_STATES: ["playing", "on"],
        CONF_GRACE_PERIOD: 120,
        CONF_AUTO_STOP: False,
        CONF_AUTO_STOP_DELAY: 1800,
        **extra,
    }


@pytest.fixture(autouse=True)
def sources_off(hass: HomeAssistant) -> None:
    """All sources are off before the stopwatch is set up."""
    for entity_id in (XBOX, PLAYSTATION, IN_GAME):
        hass.states.async_set(entity_id, "off")


async def _set(hass: HomeAssistant, entity_id: str, state: str) -> None:
    """Set the state of a source."""
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()


async def _advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock forward and run the scheduled callbacks."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _status(hass: HomeAssistant) -> str:
    """Return the status of the stopwatch."""
    return hass.states.get(STATUS).state


def _elapsed(hass: HomeAssistant) -> float:
    """Return the exact elapsed time up to the last pause."""
    return hass.states.get(ELAPSED).attributes["accumulated_seconds"]


@pytest.mark.parametrize(
    ("states", "combine", "expected"),
    [
        ([RUNNING, INACTIVE], "or", RUNNING),
        ([RUNNING, UNAVAILABLE], "or", RUNNING),
        ([INACTIVE, UNAVAILABLE], "or", UNAVAILABLE),
        ([INACTIVE, INACTIVE], "or", INACTIVE),
        ([RUNNING, RUNNING], "and", RUNNING),
        ([RUNNING, INACTIVE], "and", INACTIVE),
        ([INACTIVE, UNAVAILABLE], "and", INACTIVE),
        ([RUNNING, UNAVAILABLE], "and", UNAVAILABLE),
        ([UNAVAILABLE], "or", UNAVAILABLE),
        ([RUNNING], "and", RUNNING),
    ],
)
def test_combine_states(
    states: list[SourceState], combine: str, expected: SourceState
) -> None:
    """An unavailable source only decides when the others leave it open."""
    assert combine_states(states, combine) is expected


@pytest.mark.usefixtures("setup_stopwatch")
@pytest.mark.parametrize("options", [_options([XBOX, PLAYSTATION], "or")])
async def test_or_runs_while_one_source_runs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, events: list[Event]
) -> None:
    """With OR, a second source taking over keeps the stopwatch running."""
    await _set(hass, XBOX, "playing")
    await _advance(hass, freezer, 60)
    await _set(hass, PLAYSTATION, "playing")
    await _set(hass, XBOX, "off")
    await _advance(hass, freezer, 60)
    assert _status(hass) == "running"
    await _set(hass, PLAYSTATION, "off")
    assert _status(hass) == "paused"
    assert _elapsed(hass) == 120
    assert [event.data["type"] for event in events] == ["started", "paused"]


@pytest.mark.usefixtures("setup_stopwatch")
@pytest.mark.parametrize("options", [_options([XBOX, PLAYSTATION], "or")])
async def test_or_keeps_a_manual_pause(hass: HomeAssistant) -> None:
    """A second source starting while one runs does not undo a manual pause."""
    await _set(hass, XBOX, "playing")
    await hass.services.async_call(
        "button", "press", {"entity_id": PAUSE}, blocking=True
    )
    await _set(hass, PLAYSTATION, "playing")
    assert _status(hass) == "paused"


@pytest.mark.usefixtures("setup_stopwatch")
@pytest.mark.parametrize("options", [_options([XBOX, IN_GAME], "and")])
async def test_and_runs_only_while_all_sources_run(hass: HomeAssistant) -> None:
    """With AND, the stopwatch runs only while every source is running."""
    await _set(hass, IN_GAME, "on")
    assert _status(hass) == "idle"
    await _set(hass, XBOX, "on")
    assert _status(hass) == "running"
    await _set(hass, XBOX, "off")
    assert _status(hass) == "paused"


@pytest.mark.usefixtures("setup_stopwatch")
@pytest.mark.parametrize("options", [_options([XBOX, PLAYSTATION], "or")])
async def test_or_dropout_is_covered_by_another_source(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A dropout of one source does not matter while another one runs."""
    await _set(hass, XBOX, "playing")
    await _set(hass, PLAYSTATION, "playing")
    await _set(hass, XBOX, STATE_UNAVAILABLE)
    await _advance(hass, freezer, 600)
    assert _status(hass) == "running"


@pytest.mark.usefixtures("setup_stopwatch")
@pytest.mark.parametrize("options", [_options([XBOX, PLAYSTATION], "or")])
async def test_or_dropout_of_the_only_running_source(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """When the only running source drops out, the grace period applies."""
    await _set(hass, XBOX, "playing")
    await _advance(hass, freezer, 300)
    await _set(hass, XBOX, STATE_UNAVAILABLE)
    await _advance(hass, freezer, 60)
    assert _status(hass) == "running"
    await _advance(hass, freezer, 60)
    assert _status(hass) == "paused"
    assert _elapsed(hass) == 300


@pytest.mark.usefixtures("setup_stopwatch")
@pytest.mark.parametrize(
    "options",
    [_options([XBOX, IN_GAME], "and", **{CONF_AUTO_STOP: True})],
)
async def test_and_auto_stop(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, events: list[Event]
) -> None:
    """Auto-stop follows the combined state."""
    await _set(hass, XBOX, "on")
    await _set(hass, IN_GAME, "on")
    await _advance(hass, freezer, 600)
    await _set(hass, IN_GAME, "off")
    await _advance(hass, freezer, 1800)
    assert _status(hass) == "idle"
    assert events[-1].data["type"] == "stopped"
    assert events[-1].data["elapsed_seconds"] == 600


async def test_migration_of_a_single_source_entity(hass: HomeAssistant) -> None:
    """A stopwatch from before 1.1 gets a list with its source entity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gaming",
        data={},
        options={
            CONF_INTERVAL: 0,
            CONF_UPDATE_INTERVAL: 60,
            CONF_SOURCE_ENTITY: XBOX,
            CONF_RUNNING_STATES: ["playing"],
            CONF_GRACE_PERIOD: 120,
            CONF_AUTO_STOP: False,
            CONF_AUTO_STOP_DELAY: 1800,
        },
        minor_version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert CONF_SOURCE_ENTITY not in entry.options
    assert entry.options[CONF_SOURCE_ENTITIES] == [XBOX]
    assert entry.options[CONF_SOURCE_COMBINE] == "or"
    await _set(hass, XBOX, "playing")
    assert _status(hass) == "running"


async def test_migration_without_a_source(hass: HomeAssistant) -> None:
    """A stopwatch without a source keeps its options."""
    options = {CONF_INTERVAL: 0, CONF_UPDATE_INTERVAL: 60}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Gaming", data={}, options=options, minor_version=1
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.minor_version == 2
    assert dict(entry.options) == options


async def test_no_downgrade_from_a_newer_version(hass: HomeAssistant) -> None:
    """An entry of a newer major version is not loaded."""
    entry = MockConfigEntry(domain=DOMAIN, title="Gaming", data={}, version=2)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
