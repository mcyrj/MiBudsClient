"""Publish battery refreshes and play the app's own case-transition melodies."""

from typing import Callable

from utils.battery import battery_state
from utils.sound import play_tones


class BatteryUpdates:
    """Adapt the Bluetooth callback into UI messages without touching controls."""

    def __init__(self, publish: Callable[[dict], None]):
        self._publish = publish
        self._previous_charging_state = {"left": None, "right": None}
        self._session = None

    @staticmethod
    def get_charging_state(raw_value):
        state = battery_state(raw_value)
        return state[1] if state is not None else None

    @staticmethod
    def play_charge_transition_sound(started):
        start_pattern = [(880, 80), (1175, 90), (1568, 120)]
        stop_pattern = [(1568, 80), (1175, 90), (880, 120)]
        play_tones(start_pattern if started else stop_pattern)

    def update(self, left, right, case, *, session=None):
        if session != self._session:
            self._session = session
            self._previous_charging_state = {"left": None, "right": None}
        for side, raw_value in (("left", left), ("right", right)):
            previous = self._previous_charging_state[side]
            current = self.get_charging_state(raw_value)
            if previous is not None and current is not None and previous != current:
                self.play_charge_transition_sound(started=current)
            self._previous_charging_state[side] = current

        self._publish({
            "type": "battery",
            "left": left,
            "right": right,
            "case": case,
            "session": session,
        })
