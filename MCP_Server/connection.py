"""Socket connection to the AbletonMCP Remote Script running inside Live."""

import contextlib
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

from MCP_Server.plugin_discovery import invalidate_external_plugin_cache

logger = logging.getLogger(__name__)

ABLETON_HOST = "localhost"
ABLETON_PORT = 9877
CONNECT_TIMEOUT_SECONDS = 3.0
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_SECONDS = 0.5
PING_TIMEOUT_SECONDS = 1.0
RECEIVE_BUFFER_SIZE = 8192
RECEIVE_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 10.0
MODIFYING_TIMEOUT_SECONDS = 15.0
# Pause before and after a state-changing command so Live can process it.
MODIFYING_COMMAND_DELAY_SECONDS = 0.1

# Commands that change Live's state. Must match the Remote Script's
# MAIN_THREAD_COMMANDS; tests/unit/test_command_consistency.py enforces this.
MODIFYING_COMMANDS = (
    "create_midi_track",
    "set_track_name",
    "create_clip",
    "add_notes_to_clip",
    "set_clip_name",
    "set_tempo",
    "fire_clip",
    "stop_clip",
    "start_playback",
    "stop_playback",
    "load_browser_item",
    "set_song_time",
    "set_arrangement_loop",
    "jump_to_cue",
    "create_cue_point",
    "delete_cue_point",
    "create_arrangement_clip",
    "create_arrangement_audio_clip",
    "duplicate_to_arrangement",
    "delete_arrangement_clip",
    "set_arrangement_clip_property",
    "copy_arrangement_audio_region",
    "move_arrangement_clip",
    "set_view",
    "control_arrangement_view",
    "manage_clip_automation",
    "add_notes_to_arrangement_clip",
    "set_device_parameter",
    "set_device_enabled",
    "delete_device",
    "navigate_preset",
    "delete_track",
    "set_track_volume",
    "set_track_panning",
    "set_track_mute",
    "set_track_solo",
    "set_track_arm",
    "set_track_input",
    "set_track_monitoring",
    "set_tracks_state",
)


class AbletonError(Exception):
    """A command could not be delivered to Live or Live reported a failure."""


@dataclass
class AbletonConnection:
    """One TCP connection to the Remote Script, speaking newline-free JSON."""

    host: str
    port: int
    sock: socket.socket | None = None

    def connect(self) -> bool:
        """Connect to the Remote Script; return whether the socket is open."""
        if self.sock:
            return True
        try:
            # Bounded connect so a half-open listener can never hang the server.
            self.sock = socket.create_connection(
                (self.host, self.port), timeout=CONNECT_TIMEOUT_SECONDS
            )
            self.sock.settimeout(None)
            logger.info("Connected to Ableton at %s:%s", self.host, self.port)
            return True
        except Exception as e:
            logger.error("Failed to connect to Ableton: %s", e)
            self.sock = None
            return False

    def disconnect(self) -> None:
        """Close the socket, if open."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error("Error disconnecting from Ableton: %s", e)
            finally:
                self.sock = None

    def receive_full_response(
        self, sock: socket.socket, buffer_size: int = RECEIVE_BUFFER_SIZE
    ) -> bytes:
        """Read chunks until they form one complete JSON document."""
        chunks: list[bytes] = []
        sock.settimeout(RECEIVE_TIMEOUT_SECONDS)
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                except TimeoutError:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except ConnectionError as e:
                    logger.error("Socket connection error during receive: %s", e)
                    raise
                if not chunk:
                    if not chunks:
                        raise AbletonError("Connection closed before receiving any data")
                    break
                chunks.append(chunk)
                data = b"".join(chunks)
                try:
                    json.loads(data.decode("utf-8"))
                except json.JSONDecodeError:
                    continue  # incomplete JSON: keep receiving
                logger.info("Received complete response (%d bytes)", len(data))
                return data
        except Exception as e:
            logger.error("Error during receive: %s", e)
            raise

        # Timed out or the peer closed the socket mid-response.
        if not chunks:
            raise AbletonError("No data received")
        data = b"".join(chunks)
        logger.info("Returning data after receive completion (%d bytes)", len(data))
        try:
            json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise AbletonError("Incomplete JSON response received") from e
        return data

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> Any:
        """Send one command to Live and return its ``result`` payload.

        Raises ConnectionError when the socket cannot be opened. Any later
        failure drops the socket so the next call reconnects, and is raised
        as an AbletonError whose message the tools pass on verbatim.
        """
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {"type": command_type, "params": params or {}}
        is_modifying = command_type in MODIFYING_COMMANDS
        response_data = b""
        try:
            logger.info("Sending command: %s with params: %s", command_type, params)
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            logger.info("Command sent, waiting for response...")
            if is_modifying:
                time.sleep(MODIFYING_COMMAND_DELAY_SECONDS)
            self.sock.settimeout(
                MODIFYING_TIMEOUT_SECONDS if is_modifying else READ_TIMEOUT_SECONDS
            )

            response_data = self.receive_full_response(self.sock)
            logger.info("Received %d bytes of data", len(response_data))
            response = json.loads(response_data.decode("utf-8"))
            logger.info("Response parsed, status: %s", response.get("status", "unknown"))

            if response.get("status") == "error":
                logger.error("Ableton error: %s", response.get("message"))
                raise AbletonError(response.get("message", "Unknown error from Ableton"))

            if is_modifying:
                time.sleep(MODIFYING_COMMAND_DELAY_SECONDS)
            return response.get("result", {})
        except TimeoutError as e:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise AbletonError("Timeout waiting for Ableton response") from e
        except ConnectionError as e:
            logger.error("Socket connection error: %s", e)
            self.sock = None
            raise AbletonError(f"Connection to Ableton lost: {e}") from e
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON response from Ableton: %s", e)
            if response_data:
                logger.error("Raw response (first 200 bytes): %r", response_data[:200])
            self.sock = None
            raise AbletonError(f"Invalid response from Ableton: {e}") from e
        except Exception as e:
            logger.error("Error communicating with Ableton: %s", e)
            self.sock = None
            raise AbletonError(f"Communication error with Ableton: {e}") from e


# The one shared connection, created lazily by the first tool call.
_ableton_connection: AbletonConnection | None = None


def _drop_connection() -> None:
    """Forget the shared connection and everything cached from it."""
    global _ableton_connection
    if _ableton_connection is not None:
        _ableton_connection.disconnect()
    _ableton_connection = None
    invalidate_external_plugin_cache()


def _is_alive(conn: AbletonConnection) -> bool:
    """Probe the socket with an empty send, which Live ignores."""
    try:
        conn.sock.settimeout(PING_TIMEOUT_SECONDS)
        conn.sock.sendall(b"")
    except Exception as e:
        logger.warning("Existing connection is no longer valid: %s", e)
        return False
    return True


def close_connection() -> None:
    """Disconnect from Live on server shutdown."""
    if _ableton_connection is not None:
        logger.info("Disconnecting from Ableton on shutdown")
    _drop_connection()


def get_ableton_connection() -> AbletonConnection:
    """Return the shared connection, reconnecting (with retries) if needed."""
    global _ableton_connection

    if _ableton_connection is not None:
        if _is_alive(_ableton_connection):
            return _ableton_connection
        with contextlib.suppress(Exception):
            _ableton_connection.disconnect()
        _ableton_connection = None
        invalidate_external_plugin_cache()

    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            logger.info("Connecting to Ableton (attempt %d/%d)...", attempt, CONNECT_ATTEMPTS)
            _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
            if _ableton_connection.connect():
                logger.info("Created new persistent connection to Ableton")
                try:
                    _ableton_connection.send_command("get_session_info")
                    logger.info("Connection validated successfully")
                    return _ableton_connection
                except Exception as e:
                    logger.error("Connection validation failed: %s", e)
                    _drop_connection()
            else:
                _ableton_connection = None
        except Exception as e:
            logger.error("Connection attempt %d failed: %s", attempt, e)
            if _ableton_connection:
                _drop_connection()

        if attempt < CONNECT_ATTEMPTS:
            time.sleep(CONNECT_RETRY_DELAY_SECONDS)

    logger.error("Failed to connect to Ableton after multiple attempts")
    raise AbletonError(
        f"Could not connect to Ableton at {ABLETON_HOST}:{ABLETON_PORT}. "
        "Make sure Ableton Live is running and the AbletonMCP Remote Script is "
        "selected under Preferences > Link, Tempo & MIDI > Control Surface."
    )
