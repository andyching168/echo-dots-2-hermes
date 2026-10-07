"""Tool schemas for the hermes-echo plugin (what the model sees)."""


def _schema(name, description, properties=None, required=None):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    }


SCHEMAS = {
    "echo_get_state": _schema(
        "echo_get_state",
        "Return the authoritative Echo device state: interaction mode, physical mute, "
        "volume, whether the wake word is required, and whether a session is active. "
        "Use this before deciding how to interact with the user on the Echo.",
    ),
    "echo_get_volume": _schema(
        "echo_get_volume",
        "Return the current Echo playback volume (0-100).",
    ),
    "echo_enter_live_mode": _schema(
        "echo_enter_live_mode",
        "Enter continuous Live Mode on the Echo. Only valid from listen_one_shot or "
        "question_active. Idempotent if already in Live Mode. Rejected while the device "
        "is physically muted — there is no software unmute.",
    ),
    "echo_end_live_mode": _schema(
        "echo_end_live_mode",
        "End Live Mode and return the Echo to idle. Use this when the user clearly asks "
        "to end the conversation; do not merely claim Live Mode ended.",
        {
            "reason": {
                "type": "string",
                "enum": ["user_requested", "idle_timeout", "physical_action",
                         "remote_control", "error"],
                "description": "Why Live Mode is ending.",
            }
        },
        ["reason"],
    ),
    "echo_interrupt": _schema(
        "echo_interrupt",
        "Interrupt the current Echo interaction/output. Cancels listen/question states "
        "or marks a Live Mode turn interrupted.",
        {
            "reason": {
                "type": "string",
                "description": "Short reason for the interruption.",
            }
        },
    ),
    "echo_set_volume": _schema(
        "echo_set_volume",
        "Set the Echo playback volume (0-100, clamped). Uses the same volume "
        "implementation as the physical buttons.",
        {
            "value": {
                "type": "integer",
                "description": "Target volume 0-100.",
            }
        },
        ["value"],
    ),
    "echo_play_chime": _schema(
        "echo_play_chime",
        "Play a short acknowledgement chime on the Echo. Optional; used for feedback.",
        {
            "name": {
                "type": "string",
                "enum": ["listen", "question", "live", "idle", "interrupt", "muted", "error"],
                "description": "Which chime to play.",
            }
        },
    ),
}

#: Tools intentionally NOT exposed. Physical mute must remain non-overridable.
FORBIDDEN = ("echo_unmute", "echo_set_mic_mute", "echo_clear_mute")
