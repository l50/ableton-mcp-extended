# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
HOST = "localhost"

# Commands that modify Live's state and so run on Live's main thread. Must
# match the MCP server's MODIFYING_COMMANDS; tests/unit/test_command_consistency.py
# enforces this.
MAIN_THREAD_COMMANDS = (
    "create_midi_track", "set_track_name",
    "create_clip", "add_notes_to_clip", "set_clip_name",
    "set_tempo", "fire_clip", "stop_clip",
    "start_playback", "stop_playback", "load_browser_item",
    "set_song_time", "set_arrangement_loop", "jump_to_cue",
    "create_cue_point", "delete_cue_point",
    "create_arrangement_clip", "create_arrangement_audio_clip",
    "duplicate_to_arrangement", "delete_arrangement_clip",
    "set_arrangement_clip_property",
    "set_view", "control_arrangement_view",
    "manage_clip_automation",
    "add_notes_to_arrangement_clip",
    "set_device_parameter", "set_device_enabled",
    "delete_device", "navigate_preset",
    "delete_track",
    "set_track_volume", "set_track_panning",
    "set_track_mute", "set_track_solo", "set_track_arm",
    "set_track_input", "set_track_monitoring", "set_tracks_state",
)

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP Remote Script initializing...")
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()
        
        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Route the command to the appropriate handler
            if command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                include_clip_slots = params.get("include_clip_slots", True)
                response["result"] = self._get_track_info(
                    track_index, include_clip_slots)
            elif command_type == "get_track_routing_options":
                response["result"] = self._get_track_routing_options(params)
            elif command_type == "get_recording_status":
                response["result"] = self._get_recording_status()
            elif command_type == "list_tracks":
                response["result"] = self._list_tracks(
                    params.get("include_returns", True),
                    params.get("include_master", True))
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in MAIN_THREAD_COMMANDS:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            result = self._load_browser_item(track_index, item_uri)
                        elif command_type == "set_song_time":
                            time_val = params.get("time", 0.0)
                            result = self._set_song_time(time_val)
                        elif command_type == "set_arrangement_loop":
                            enabled = params.get("enabled", True)
                            start = params.get("start", None)
                            length = params.get("length", None)
                            result = self._set_arrangement_loop(enabled, start, length)
                        elif command_type == "jump_to_cue":
                            direction = params.get("direction", None)
                            name = params.get("name", None)
                            result = self._jump_to_cue(direction, name)
                        elif command_type == "create_cue_point":
                            time_val = params.get("time", 0.0)
                            name = params.get("name", "")
                            result = self._create_cue_point(time_val, name)
                        elif command_type == "delete_cue_point":
                            time_val = params.get("time", 0.0)
                            result = self._delete_cue_point(time_val)
                        elif command_type == "create_arrangement_clip":
                            ti = params.get("track_index", 0)
                            pos = params.get("position", 0.0)
                            length = params.get("length", 4.0)
                            result = self._create_arrangement_clip(ti, pos, length)
                        elif command_type == "create_arrangement_audio_clip":
                            ti = params.get("track_index", 0)
                            pos = params.get("position", 0.0)
                            fp = params.get("file_path", "")
                            result = self._create_arrangement_audio_clip(ti, pos, fp)
                        elif command_type == "duplicate_to_arrangement":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", 0)
                            dt = params.get("destination_time", 0.0)
                            result = self._duplicate_to_arrangement(ti, ci, dt)
                        elif command_type == "delete_arrangement_clip":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", None)
                            cn = params.get("clip_name", None)
                            result = self._delete_arrangement_clip(ti, ci, cn)
                        elif command_type == "set_arrangement_clip_property":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", None)
                            cn = params.get("clip_name", None)
                            prop = params.get("property", "")
                            val = params.get("value", None)
                            result = self._set_arrangement_clip_property(ti, ci, prop, val, cn)
                        elif command_type == "set_view":
                            vn = params.get("view_name", "Arranger")
                            result = self._set_view(vn)
                        elif command_type == "control_arrangement_view":
                            action = params.get("action", "")
                            ti = params.get("track_index", 0)
                            result = self._control_arrangement_view(action, ti)
                        elif command_type == "manage_clip_automation":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", None)
                            cn = params.get("clip_name", None)
                            action = params.get("action", "create")
                            pn = params.get("parameter_name", "")
                            result = self._manage_clip_automation(ti, ci, action, pn, cn)
                        elif command_type == "add_notes_to_arrangement_clip":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_arrangement_clip(ti, ci, notes)
                        # Device modifying commands
                        elif command_type == "set_device_parameter":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            ci = params.get("chain_index", None)
                            pn = params.get("parameter_name", None)
                            pi = params.get("parameter_index", None)
                            val = params.get("value", 0.0)
                            result = self._set_device_parameter(ti, di, ci, pn, pi, val)
                        elif command_type == "set_device_enabled":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            ci = params.get("chain_index", None)
                            enabled = params.get("enabled", True)
                            result = self._set_device_enabled(ti, di, ci, enabled)
                        elif command_type == "delete_device":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            result = self._delete_device(ti, di)
                        elif command_type == "delete_track":
                            ti = params.get("track_index", 0)
                            result = self._delete_track(ti)
                        elif command_type == "set_track_volume":
                            ti = params.get("track_index", 0)
                            volume = params.get("volume", 0.85)
                            result = self._set_track_volume(ti, volume)
                        elif command_type == "set_track_panning":
                            ti = params.get("track_index", 0)
                            panning = params.get("panning", 0.0)
                            result = self._set_track_panning(ti, panning)
                        elif command_type == "set_track_mute":
                            result = self._set_track_mute(params)
                        elif command_type == "set_track_solo":
                            result = self._set_track_solo(params)
                        elif command_type == "set_track_arm":
                            result = self._set_track_arm(params)
                        elif command_type == "set_track_input":
                            result = self._set_track_input(params)
                        elif command_type == "set_track_monitoring":
                            result = self._set_track_monitoring(params)
                        elif command_type == "set_tracks_state":
                            result = self._set_tracks_state(params)
                        elif command_type == "navigate_preset":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            ci = params.get("chain_index", None)
                            direction = params.get("direction", "current")
                            result = self._navigate_preset(ti, di, ci, direction)

                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # Wait for the response with a timeout
                try:
                    task_response = response_queue.get(timeout=10.0)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_track_volume":
                ti = params.get("track_index", 0)
                response["result"] = self._get_track_volume(ti)
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                response["result"] = self.get_browser_items_at_path(path)
            elif command_type == "get_arrangement_info":
                track_index = params.get("track_index", -1)
                response["result"] = self._get_arrangement_info(track_index)
            elif command_type == "get_cue_points":
                response["result"] = self._get_cue_points()
            # Device read-only commands
            elif command_type == "get_device_parameters":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                ci = params.get("chain_index", None)
                show_all = params.get("show_all", False)
                response["result"] = self._get_device_parameters(ti, di, ci, show_all)
            elif command_type == "get_chain_info":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                ci = params.get("chain_index", None)
                response["result"] = self._get_chain_info(ti, di, ci)
            elif command_type == "get_drum_pad_info":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                response["result"] = self._get_drum_pad_info(ti, di)
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Arrangement helper methods

    def _get_arrangement_clip_info(self, clip):
        """Serialize a Clip object to ArrangementClipInfo dict."""
        try:
            return {
                "name": clip.name,
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "length": clip.end_time - clip.start_time,
                "is_midi": clip.is_midi_clip,
                "is_audio": clip.is_audio_clip,
                "muted": clip.muted,
                "color": clip.color,
                "looping": clip.looping,
                "loop_start": clip.loop_start,
                "loop_end": clip.loop_end,
            }
        except Exception as e:
            self.log_message("Error getting arrangement clip info: " + str(e))
            return {"name": "unknown", "error": str(e)}

    def _get_transport_info(self):
        """Serialize Song transport state to TransportInfo dict."""
        try:
            return {
                "is_playing": self._song.is_playing,
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "current_time": self._song.current_song_time,
                "song_length": self._song.song_length,
                "loop_enabled": self._song.loop,
                "loop_start": self._song.loop_start,
                "loop_length": self._song.loop_length,
                "arrangement_overdub": self._song.arrangement_overdub,
                "back_to_arranger": self._song.back_to_arranger,
            }
        except Exception as e:
            self.log_message("Error getting transport info: " + str(e))
            raise

    def _resolve_arrangement_clip(self, track_index, clip_index=None, clip_name=None):
        """Resolve an arrangement clip by index or name.

        Returns (track, clip) tuple.
        """
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index {0} out of range (0-{1})".format(
                track_index, len(self._song.tracks) - 1))

        track = self._song.tracks[track_index]
        clips = track.arrangement_clips

        if clip_name:
            matches = [(i, c) for i, c in enumerate(clips) if c.name == clip_name]
            if len(matches) == 0:
                raise ValueError("No arrangement clip named '{0}' on track '{1}'".format(
                    clip_name, track.name))
            if len(matches) > 1:
                raise ValueError("Ambiguous: {0} clips named '{1}' on track '{2}'".format(
                    len(matches), clip_name, track.name))
            return track, matches[0][1]

        if clip_index is None:
            raise ValueError("Either clip_index or clip_name must be provided")

        if clip_index < 0 or clip_index >= len(clips):
            raise IndexError("Clip index {0} out of range (0-{1}) on track '{2}'".format(
                clip_index, len(clips) - 1, track.name))

        return track, clips[clip_index]

    def _check_overlap(self, track, position, length):
        """Return list of clip names that overlap with [position, position+length)."""
        overlapped = []
        end = position + length
        for clip in track.arrangement_clips:
            if clip.start_time < end and clip.end_time > position:
                overlapped.append(clip.name or "(unnamed)")
        return overlapped

    # Command implementations

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                }
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index, include_clip_slots=True):
        """Get information about a track, including its routing, monitoring,
        arm and group state. include_clip_slots=False leaves out the session
        clip slots, which are the bulk of the reply on large Sets."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(
                    track.clip_slots if include_clip_slots else []):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            is_group = bool(getattr(track, "is_foldable", False))

            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "is_group_track": is_group,
                "mute": track.mute,
                "solo": track.solo,
                "arm": None if is_group else track.arm,
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            if not include_clip_slots:
                del result["clip_slots"]

            group = self._safe_attr(track, "group_track")
            is_grouped = bool(self._safe_attr(track, "is_grouped", False))
            group_ref = None
            if is_grouped and group is not None:
                group_ref = {"index": self._index_of(group, self._song.tracks),
                             "name": group.name}
            take_lanes = self._safe_attr(track, "take_lanes")
            result.update({
                "input": self._routing(track, "input"),
                "output": self._routing(track, "output"),
                "monitoring": self._monitoring_name(track),
                "can_be_armed": self._can_arm(track),
                "implicit_arm": self._safe_attr(track, "implicit_arm"),
                "is_foldable": is_group,
                "fold_state": (bool(self._safe_attr(track, "fold_state", False))
                               if is_group else None),
                "is_grouped": is_grouped,
                "group_track": group_ref,
                "is_visible": self._safe_attr(track, "is_visible"),
                "muted_via_solo": self._safe_attr(track, "muted_via_solo"),
                "is_frozen": self._safe_attr(track, "is_frozen"),
                "color": self._safe_attr(track, "color"),
                "arrangement_clip_count": self._arrangement_clip_count(track),
                "take_lane_count": (len(list(take_lanes))
                                    if take_lanes is not None else None),
            })
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise
    
    
    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise

    def _get_track_volume(self, track_index):
        """Get current volume and panning for a track's mixer fader."""
        try:
            all_tracks = list(self._song.tracks) + list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(all_tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(all_tracks) - 1))
            track = all_tracks[track_index]
            vol_param = track.mixer_device.volume
            pan_param = track.mixer_device.panning
            return {
                "track_name": track.name,
                "volume": vol_param.value,
                "volume_min": vol_param.min,
                "volume_max": vol_param.max,
                "panning": pan_param.value,
                "panning_min": pan_param.min,
                "panning_max": pan_param.max,
            }
        except Exception as e:
            self.log_message("Error getting track volume: " + str(e))
            raise

    def _set_track_volume(self, track_index, volume):
        """Set the mixer fader volume for a track.
        
        Args:
            track_index: 0-based track index (includes return tracks after session tracks)
            volume: normalized 0.0 (silence) to 1.0 (max). 0.85 = 0dB unity gain.
        """
        try:
            all_tracks = list(self._song.tracks) + list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(all_tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(all_tracks) - 1))
            track = all_tracks[track_index]
            vol_param = track.mixer_device.volume
            # Clamp to valid range
            clamped = max(vol_param.min, min(vol_param.max, float(volume)))
            vol_param.value = clamped
            return {
                "track_name": track.name,
                "volume": vol_param.value,
            }
        except Exception as e:
            self.log_message("Error setting track volume: " + str(e))
            raise

    def _set_track_panning(self, track_index, panning):
        """Set the mixer panning for a track.
        
        Args:
            track_index: 0-based track index
            panning: -1.0 (full left) to +1.0 (full right), 0.0 = center
        """
        try:
            all_tracks = list(self._song.tracks) + list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(all_tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(all_tracks) - 1))
            track = all_tracks[track_index]
            pan_param = track.mixer_device.panning
            clamped = max(pan_param.min, min(pan_param.max, float(panning)))
            pan_param.value = clamped
            return {
                "track_name": track.name,
                "panning": pan_param.value,
            }
        except Exception as e:
            self.log_message("Error setting track panning: " + str(e))
            raise

    # Track addressing and recording-safety helpers
    #
    # Every handler below addresses its track through _resolve_track, so a
    # command can name a track by index, by name, or (where it makes sense) as
    # a return track, and can carry an expect_name guard. Indices on the wire
    # are 0-based like the rest of this script, but error messages quote the
    # 1-based numbers the MCP tools show to callers.

    _TRACK_REF_KEYS = ("track_index", "track_name", "return_track_index",
                       "expect_name")
    _TRACK_STATE_KEYS = ("mute", "solo", "arm", "input_type", "input_channel",
                         "monitoring")
    _NO_INPUT = "no input"

    @staticmethod
    def _safe_attr(obj, name, default=None):
        """getattr that also swallows the RuntimeError Live raises for
        properties a track kind does not have (e.g. arm on a group)."""
        try:
            return getattr(obj, name, default)
        except Exception:
            return default

    @staticmethod
    def _return_track_label(index):
        return chr(ord("A") + index)

    @staticmethod
    def _routing_name(routing):
        if routing is None:
            return None
        return getattr(routing, "display_name", None)

    def _index_of(self, track, pool):
        for i, candidate in enumerate(pool):
            if candidate == track:
                return i
        return None

    def _track_ref(self, track):
        """Compact reference to a track: index for tracks, letter for returns."""
        index = self._index_of(track, self._song.tracks)
        if index is not None:
            return {"index": index, "name": track.name}
        index = self._index_of(track, self._song.return_tracks)
        if index is not None:
            return {"return_track": self._return_track_label(index),
                    "name": track.name}
        return {"master": True, "name": track.name}

    def _describe_track(self, track):
        index = self._index_of(track, self._song.tracks)
        if index is not None:
            return "'{0}' (track_index {1})".format(track.name, index + 1)
        index = self._index_of(track, self._song.return_tracks)
        return "'{0}' (return_track {1})".format(
            track.name, self._return_track_label(index))

    def _check_expect_name(self, track, expect_name):
        """Fail when the addressed track is not the one the caller expects.

        Track indices shift whenever tracks are inserted or groups change, so
        a write that carries expect_name refuses to touch a different track.
        """
        if expect_name in (None, ""):
            return
        actual = track.name or ""
        if actual.lower() != str(expect_name).lower():
            raise ValueError(
                "expect_name mismatch: expected '{0}' but the addressed track "
                "is '{1}'; nothing was changed".format(expect_name, actual))

    def _resolve_track(self, params, allow_returns=False):
        """Return the Track that params address.

        params holds exactly one of track_index (0-based over song.tracks,
        groups included), track_name (case-insensitive exact match) or
        return_track_index (0-based over song.return_tracks, only when
        allow_returns). An optional expect_name must match the resolved
        track's name. Raises IndexError or ValueError naming the problem.
        """
        tracks = list(self._song.tracks)
        returns = list(self._song.return_tracks)
        track_index = params.get("track_index")
        track_name = params.get("track_name") or ""
        return_index = params.get("return_track_index")

        given = []
        if track_index is not None:
            given.append("track_index")
        if track_name:
            given.append("track_name")
        if return_index is not None:
            given.append("return_track_index")
        if len(given) != 1:
            raise ValueError(
                "Give exactly one of track_index, track_name or return_track "
                "(got {0})".format(", ".join(given) or "none"))

        if track_index is not None:
            track_index = int(track_index)
            if track_index < 0 or track_index >= len(tracks):
                raise IndexError(
                    "track_index {0} is out of range: the Set has {1} "
                    "track(s) (1-{1})".format(track_index + 1, len(tracks)))
            track = tracks[track_index]
        elif return_index is not None:
            if not allow_returns:
                raise ValueError(
                    "This command does not apply to return tracks")
            return_index = int(return_index)
            if return_index < 0 or return_index >= len(returns):
                raise IndexError(
                    "return_track {0} does not exist: the Set has {1} return "
                    "track(s)".format(return_index + 1, len(returns)))
            track = returns[return_index]
        else:
            wanted = track_name.lower()
            pool = tracks + (returns if allow_returns else [])
            matches = [t for t in pool if (t.name or "").lower() == wanted]
            if not matches:
                raise ValueError("No track named '{0}'. Tracks: {1}".format(
                    track_name, ", ".join(
                        "'{0}'".format(t.name) for t in pool) or "(none)"))
            if len(matches) > 1:
                raise ValueError(
                    "Track name '{0}' is ambiguous; {1} tracks match: {2}. "
                    "Use track_index or return_track instead.".format(
                        track_name, len(matches), ", ".join(
                            self._describe_track(t) for t in matches)))
            track = matches[0]

        self._check_expect_name(track, params.get("expect_name"))
        return track

    def _is_return(self, track):
        return self._index_of(track, self._song.return_tracks) is not None

    def _is_group(self, track):
        return bool(self._safe_attr(track, "is_foldable", False))

    def _can_arm(self, track):
        if self._is_group(track) or self._is_return(track):
            return False
        return bool(self._safe_attr(track, "can_be_armed", True))

    def _require_armable(self, track):
        if self._is_group(track):
            raise ValueError(
                "Track '{0}' is a group track and has no Arm state".format(
                    track.name))
        if not self._can_arm(track):
            raise ValueError(
                "Track '{0}' cannot be armed (group, return and master tracks "
                "have no Arm state)".format(track.name))

    def _track_kind(self, track):
        if self._is_return(track):
            return "return"
        if self._index_of(track, self._song.tracks) is None:
            return "master"
        if self._is_group(track):
            return "group"
        if self._safe_attr(track, "has_midi_input", False):
            return "midi"
        if self._safe_attr(track, "has_audio_input", False):
            return "audio"
        return "unknown"

    def _routing(self, track, direction):
        current = self._safe_attr(track, direction + "_routing_type")
        if current is None:
            return None
        return {
            "type": self._routing_name(current),
            "channel": self._routing_name(
                self._safe_attr(track, direction + "_routing_channel")),
        }

    def _monitoring_states(self):
        """Map 'in'/'auto'/'off' to Live's Track.monitoring_states values."""
        try:
            import Live
            states = Live.Track.Track.monitoring_states
            return {"in": states.IN, "auto": states.AUTO, "off": states.OFF}
        except Exception:
            # Live's enum values; used when the Live module is not importable.
            return {"in": 0, "auto": 1, "off": 2}

    def _monitoring_name(self, track):
        value = self._safe_attr(track, "current_monitoring_state")
        if value is None:
            return None
        for name, state in self._monitoring_states().items():
            if value == state:
                return name
        return str(value)

    def _require_monitorable(self, track):
        if self._is_group(track):
            raise ValueError(
                "Track '{0}' is a group track and has no monitoring "
                "state".format(track.name))
        if not self._can_arm(track):
            raise ValueError(
                "Track '{0}' has no monitoring state (return and master "
                "tracks cannot be monitored)".format(track.name))
        if not hasattr(track, "current_monitoring_state"):
            raise ValueError(
                "Monitoring is not available in this Live version")
        if self._safe_attr(track, "is_frozen", False):
            raise ValueError(
                "Track '{0}' is frozen; unfreeze it before changing "
                "monitoring".format(track.name))

    def _require_input_routing(self, track):
        if self._is_group(track):
            raise ValueError(
                "Track '{0}' is a group track and has no input "
                "routing".format(track.name))
        if self._is_return(track):
            raise ValueError(
                "Track '{0}' is a return track and has no input "
                "routing".format(track.name))
        if not hasattr(track, "available_input_routing_types"):
            raise ValueError(
                "Input routing is not available in this Live version")

    def _match_routing(self, options, wanted, label):
        wanted_lower = wanted.lower()
        for option in options:
            if (self._routing_name(option) or "").lower() == wanted_lower:
                return option
        raise ValueError("No {0} named '{1}'. Valid: {2}".format(
            label, wanted, ", ".join(
                "'{0}'".format(self._routing_name(o)) for o in options)
            or "(none)"))

    def _apply_input(self, track, input_type, input_channel):
        """Set the input type (and optionally channel) by display name.

        The type is assigned only when it differs from the current one, so a
        retry that names the current type only changes the channel. When the
        channel is not offered after the type change, the previous type and
        channel are restored before the error is raised.
        """
        target = self._match_routing(
            list(track.available_input_routing_types), input_type,
            "input type")
        previous_type = self._safe_attr(track, "input_routing_type")
        previous_channel = self._safe_attr(track, "input_routing_channel")
        type_changed = ((self._routing_name(previous_type) or "").lower()
                        != input_type.lower())
        if type_changed:
            track.input_routing_type = target
        if not input_channel:
            return
        channels = list(self._safe_attr(
            track, "available_input_routing_channels", []) or [])
        try:
            channel = self._match_routing(channels, input_channel,
                                          "input channel")
        except ValueError as e:
            if type_changed:
                track.input_routing_type = previous_type
                if previous_channel is not None:
                    track.input_routing_channel = previous_channel
                raise ValueError(
                    "{0} for input type '{1}'. The input was left "
                    "unchanged. Set input_type on its own first, then call "
                    "again to change only input_channel (naming the same "
                    "input_type).".format(e, input_type))
            raise
        track.input_routing_channel = channel

    def _arm_warnings(self, track, include_others=True):
        warnings = []
        armed = bool(self._safe_attr(track, "arm", False))
        if armed:
            current_input = self._routing(track, "input")
            if (current_input and (current_input.get("type") or "").lower()
                    == self._NO_INPUT):
                warnings.append(
                    "'{0}' is armed with input 'No Input', so it records "
                    "nothing".format(track.name))
            if self._monitoring_name(track) == "in":
                warnings.append(
                    "'{0}' has monitoring 'In', so its input is always "
                    "audible".format(track.name))
            if include_others:
                others = [t.name for t in self._song.tracks
                          if t != track and self._can_arm(t)
                          and self._safe_attr(t, "arm", False)]
                if others:
                    warnings.append("Other tracks are still armed: {0}".format(
                        ", ".join(others)))
        if self._safe_attr(track, "implicit_arm", False):
            warnings.append(
                "'{0}' has implicit_arm set (Live's auto-arm on selection), "
                "so it can record even when arm is off".format(track.name))
        return warnings

    def _track_state(self, track):
        """Mixer and recording state, as reported by set_tracks_state."""
        return {
            "mute": self._safe_attr(track, "mute"),
            "solo": self._safe_attr(track, "solo"),
            "arm": (bool(self._safe_attr(track, "arm", False))
                    if self._can_arm(track) else None),
            "input": self._routing(track, "input"),
            "monitoring": self._monitoring_name(track),
        }

    def _raw_state(self, track):
        """Raw Live values captured before a batch, for rollback."""
        raw = {"mute": self._safe_attr(track, "mute"),
               "solo": self._safe_attr(track, "solo")}
        if self._can_arm(track):
            raw["arm"] = self._safe_attr(track, "arm")
        for attr in ("input_routing_type", "input_routing_channel",
                     "current_monitoring_state"):
            value = self._safe_attr(track, attr)
            if value is not None:
                raw[attr] = value
        return raw

    def _restore_raw(self, track, raw):
        errors = []
        for attr in ("input_routing_type", "input_routing_channel",
                     "current_monitoring_state", "mute", "solo", "arm"):
            if attr not in raw or raw[attr] is None:
                continue
            try:
                if self._safe_attr(track, attr) != raw[attr]:
                    setattr(track, attr, raw[attr])
            except Exception as e:
                errors.append("{0}.{1}: {2}".format(track.name, attr, e))
        return errors

    def _arrangement_clip_count(self, track):
        try:
            return len(list(track.arrangement_clips))
        except Exception:
            return None

    # Recording-safety command implementations

    def _set_track_mute(self, params):
        """Mute or unmute a track or return track."""
        try:
            track = self._resolve_track(params, allow_returns=True)
            track.mute = bool(params.get("mute", True))
            return {
                "track": self._track_ref(track),
                "mute": bool(track.mute),
                "solo": bool(self._safe_attr(track, "solo", False)),
                "muted_via_solo": bool(
                    self._safe_attr(track, "muted_via_solo", False)),
            }
        except Exception as e:
            self.log_message("Error setting track mute: " + str(e))
            raise

    def _set_track_solo(self, params):
        """Solo or unsolo a track or return track.

        Live's API bypasses exclusive solo, so with exclusive=True this
        unsolos every other track and return track itself, in the same task.
        """
        try:
            track = self._resolve_track(params, allow_returns=True)
            solo = bool(params.get("solo", True))
            unsoloed = []
            if solo and params.get("exclusive", False):
                pool = list(self._song.tracks) + list(self._song.return_tracks)
                for other in pool:
                    if other != track and self._safe_attr(other, "solo",
                                                              False):
                        other.solo = False
                        unsoloed.append(other.name)
            track.solo = solo
            return {
                "track": self._track_ref(track),
                "solo": bool(track.solo),
                "unsoloed": unsoloed,
            }
        except Exception as e:
            self.log_message("Error setting track solo: " + str(e))
            raise

    def _set_track_arm(self, params):
        """Arm or disarm a track for recording.

        Group, return and master tracks have no Arm state, and touching
        track.arm on one raises inside Live, so they are refused with a clear
        message first. Frozen tracks cannot be armed. With exclusive=True,
        every other armed track is disarmed in the same main-thread task.
        """
        try:
            track = self._resolve_track(params)
            arm = bool(params.get("arm", True))
            self._require_armable(track)
            if arm and self._safe_attr(track, "is_frozen", False):
                raise ValueError(
                    "Track '{0}' is frozen; unfreeze it before "
                    "arming".format(track.name))
            disarmed = []
            if arm and params.get("exclusive", False):
                for other in self._song.tracks:
                    if (other != track and self._can_arm(other)
                            and self._safe_attr(other, "arm", False)):
                        other.arm = False
                        disarmed.append(other.name)
            track.arm = arm
            return {
                "track": self._track_ref(track),
                "arm": bool(track.arm),
                "implicit_arm": bool(
                    self._safe_attr(track, "implicit_arm", False)),
                "input": self._routing(track, "input"),
                "monitoring": self._monitoring_name(track),
                "disarmed": disarmed,
                "warnings": self._arm_warnings(track),
            }
        except Exception as e:
            self.log_message("Error setting track arm: " + str(e))
            raise

    def _get_track_routing_options(self, params):
        """Current input/output routing plus the names Live offers."""
        try:
            track = self._resolve_track(params, allow_returns=True)
            result = {"track": self._track_ref(track)}
            for direction in ("input", "output"):
                current = self._routing(track, direction)
                types = list(self._safe_attr(
                    track, "available_{0}_routing_types".format(direction),
                    []) or [])
                if current is None and not types:
                    result[direction] = None
                    continue
                channels = list(self._safe_attr(
                    track, "available_{0}_routing_channels".format(direction),
                    []) or [])
                result[direction] = {
                    "current": current,
                    "types": [self._routing_name(t) for t in types],
                    "channels": [self._routing_name(c) for c in channels],
                }
            return result
        except Exception as e:
            self.log_message("Error getting routing options: " + str(e))
            raise

    def _set_track_input(self, params):
        """Set a track's input type and channel by display name."""
        try:
            track = self._resolve_track(params)
            input_type = str(params.get("input_type") or "")
            input_channel = str(params.get("input_channel") or "")
            if not input_type:
                raise ValueError("input_type is required")
            self._require_input_routing(track)
            self._apply_input(track, input_type, input_channel)
            warnings = []
            current_input = self._routing(track, "input")
            if (((current_input or {}).get("type") or "").lower()
                    == self._NO_INPUT
                    and self._can_arm(track)
                    and self._safe_attr(track, "arm", False)):
                warnings.append(
                    "'{0}' is still armed with input 'No Input'; disarm it "
                    "if it should not record".format(track.name))
            return {
                "track": self._track_ref(track),
                "input": current_input,
                "available_channels": [
                    self._routing_name(c) for c in list(self._safe_attr(
                        track, "available_input_routing_channels", [])
                        or [])],
                "arm": (bool(self._safe_attr(track, "arm", False))
                        if self._can_arm(track) else None),
                "warnings": warnings,
            }
        except Exception as e:
            self.log_message("Error setting track input: " + str(e))
            raise

    def _set_track_monitoring(self, params):
        """Set monitoring to 'in', 'auto' or 'off'."""
        try:
            track = self._resolve_track(params)
            state = str(params.get("state") or "").strip().lower()
            states = self._monitoring_states()
            if state not in states:
                raise ValueError(
                    "state must be one of in, auto, off (got '{0}')".format(
                        params.get("state")))
            self._require_monitorable(track)
            track.current_monitoring_state = states[state]
            return {
                "track": self._track_ref(track),
                "monitoring": self._monitoring_name(track),
            }
        except Exception as e:
            self.log_message("Error setting track monitoring: " + str(e))
            raise

    def _plan_track_change(self, change, seen, number):
        """Resolve and validate one set_tracks_state entry without writing.

        Every problem found in the entry is raised together in one ValueError.
        """
        if not isinstance(change, dict):
            raise ValueError("each change must be an object")
        issues = []
        unknown = sorted(set(change) - set(self._TRACK_REF_KEYS)
                         - set(self._TRACK_STATE_KEYS))
        if unknown:
            issues.append("unknown key(s): {0}".format(", ".join(unknown)))
        requested = dict((k, change[k]) for k in self._TRACK_STATE_KEYS
                         if change.get(k) is not None)
        if not requested:
            issues.append("nothing to change; give mute, solo, arm, "
                          "input_type, input_channel or monitoring")
        for key in ("mute", "solo", "arm"):
            if key in requested and not isinstance(requested[key], bool):
                issues.append("{0} must be true or false".format(key))
        for key in ("input_type", "input_channel"):
            if key in requested:
                requested[key] = str(requested[key])
        if "monitoring" in requested:
            state = str(requested["monitoring"]).strip().lower()
            if state in self._monitoring_states():
                requested["monitoring"] = state
            else:
                issues.append("monitoring must be one of in, auto, off "
                              "(got '{0}')".format(requested["monitoring"]))

        try:
            track = self._resolve_track(change, allow_returns=True)
        except (ValueError, IndexError, TypeError) as e:
            raise ValueError("; ".join(issues + [str(e)]))
        earlier = [n for other, n in seen if other == track]
        if earlier:
            issues.append("'{0}' is already changed by change {1}".format(
                track.name, earlier[0]))
        else:
            seen.append((track, number))

        if "arm" in requested:
            try:
                self._require_armable(track)
            except ValueError as e:
                issues.append(str(e))
            else:
                if (requested["arm"] is True
                        and self._safe_attr(track, "is_frozen", False)):
                    issues.append("Track '{0}' is frozen; unfreeze it before "
                                  "arming".format(track.name))
        if "input_type" in requested or "input_channel" in requested:
            try:
                self._require_input_routing(track)
                current_type = self._routing_name(
                    self._safe_attr(track, "input_routing_type")) or ""
                input_type = requested.get("input_type") or current_type
                if "input_type" in requested:
                    self._match_routing(
                        list(track.available_input_routing_types),
                        input_type, "input type")
                # A channel can be checked up front only when the type stays
                # the same; after a type change Live offers a new channel list,
                # which is checked during the apply (and rolled back on error).
                if (requested.get("input_channel")
                        and input_type.lower() == current_type.lower()):
                    self._match_routing(
                        list(self._safe_attr(
                            track, "available_input_routing_channels", [])
                            or []),
                        requested["input_channel"], "input channel")
            except ValueError as e:
                issues.append(str(e))
        if "monitoring" in requested:
            try:
                self._require_monitorable(track)
            except ValueError as e:
                issues.append(str(e))
        if issues:
            raise ValueError("; ".join(issues))
        return {
            "track": track,
            "requested": requested,
            "before": self._track_state(track),
            "raw": self._raw_state(track),
        }

    def _apply_track_change(self, plan):
        track = plan["track"]
        requested = plan["requested"]
        if "input_type" in requested or "input_channel" in requested:
            current_type = self._routing_name(
                self._safe_attr(track, "input_routing_type")) or ""
            self._apply_input(track, requested.get("input_type") or current_type,
                              requested.get("input_channel") or "")
        if "monitoring" in requested:
            states = self._monitoring_states()
            track.current_monitoring_state = states[requested["monitoring"]]
        if "mute" in requested:
            track.mute = requested["mute"]
        if "solo" in requested:
            track.solo = requested["solo"]
        if "arm" in requested:
            track.arm = requested["arm"]

    def _set_tracks_state(self, params):
        """Change several tracks' state in one validated batch.

        Every entry is resolved and validated before anything is written; any
        problem rejects the whole batch with every problem listed. With
        dry_run=True nothing is written and the resolved targets are
        returned. If Live rejects a write part-way, every track in the batch
        is restored to its earlier state before the error is raised.
        """
        try:
            changes = params.get("changes")
            dry_run = bool(params.get("dry_run", False))
            if not isinstance(changes, list) or not changes:
                raise ValueError("changes must be a non-empty list")
            plans, problems, seen = [], [], []
            for number, change in enumerate(changes, 1):
                try:
                    plans.append(self._plan_track_change(change, seen, number))
                except (ValueError, IndexError, TypeError) as e:
                    problems.append("change {0}: {1}".format(number, e))
            if problems:
                raise ValueError(
                    "Rejected the whole batch; nothing was changed. " +
                    "; ".join(problems))

            if dry_run:
                return {
                    "dry_run": True,
                    "changes": [{"track": self._track_ref(p["track"]),
                                 "before": p["before"],
                                 "requested": p["requested"]}
                                for p in plans],
                    "warnings": [],
                }

            applied = []
            try:
                for plan in plans:
                    applied.append(plan)
                    self._apply_track_change(plan)
            except Exception as e:
                rollback_errors = []
                for plan in reversed(applied):
                    rollback_errors.extend(
                        self._restore_raw(plan["track"], plan["raw"]))
                message = ("Live rejected the change to '{0}': {1}. Every "
                           "track in the batch was restored".format(
                               applied[-1]["track"].name, e))
                if rollback_errors:
                    message += ", except: " + "; ".join(rollback_errors)
                raise RuntimeError(message)

            warnings = []
            for plan in plans:
                if "arm" in plan["requested"] or "input_type" in plan["requested"]:
                    warnings.extend(self._arm_warnings(plan["track"],
                                                       include_others=False))
            return {
                "dry_run": False,
                "changes": [{"track": self._track_ref(p["track"]),
                             "before": p["before"],
                             "after": self._track_state(p["track"])}
                            for p in plans],
                "warnings": warnings,
            }
        except Exception as e:
            self.log_message("Error setting tracks state: " + str(e))
            raise

    def _get_recording_status(self):
        """Pre-recording check: transport flags, armed tracks and warnings."""
        try:
            song = self._song
            transport = {}
            for key in ("is_playing", "record_mode", "session_record",
                        "arrangement_overdub", "punch_in", "punch_out",
                        "metronome", "count_in_duration", "loop",
                        "exclusive_arm", "exclusive_solo"):
                transport[key] = self._safe_attr(song, key)

            armed_tracks, warnings, by_input = [], [], {}
            for index, track in enumerate(song.tracks):
                if not self._can_arm(track):
                    continue
                arm = bool(self._safe_attr(track, "arm", False))
                implicit = bool(self._safe_attr(track, "implicit_arm", False))
                current_input = self._routing(track, "input")
                monitoring = self._monitoring_name(track)
                if arm or implicit:
                    armed_tracks.append({
                        "index": index, "name": track.name, "arm": arm,
                        "implicit_arm": implicit, "input": current_input,
                        "monitoring": monitoring})
                input_type = ((current_input or {}).get("type") or "")
                if arm and input_type.lower() == self._NO_INPUT:
                    warnings.append("'{0}' is armed with input 'No Input', "
                                    "so it records nothing".format(track.name))
                elif arm and current_input:
                    key = (input_type, current_input.get("channel"))
                    by_input.setdefault(key, []).append(track.name)
                if implicit:
                    warnings.append(
                        "'{0}' has implicit_arm set (Live's auto-arm on "
                        "selection), so it can record even when arm is "
                        "off".format(track.name))
                if monitoring == "in" and not arm:
                    warnings.append(
                        "'{0}' has monitoring 'In' while not armed, so its "
                        "input is audible".format(track.name))
            for (input_type, channel), names in sorted(by_input.items()):
                if len(names) > 1:
                    warnings.append(
                        "{0} armed tracks share input '{1}' / '{2}': "
                        "{3}".format(len(names), input_type, channel,
                                     ", ".join(names)))
            if transport["record_mode"] and not transport["is_playing"]:
                warnings.append("Arrangement record is on while the "
                                "transport is stopped; the next play "
                                "records")
            return {"transport": transport, "armed_tracks": armed_tracks,
                    "warnings": warnings}
        except Exception as e:
            self.log_message("Error getting recording status: " + str(e))
            raise

    def _track_row(self, track, index):
        is_group = self._is_group(track)
        armable = self._can_arm(track)
        group = self._safe_attr(track, "group_track")
        return {
            "index": index,
            "name": track.name,
            "kind": self._track_kind(track),
            "group": (group.name if group is not None
                      and self._safe_attr(track, "is_grouped", False)
                      else None),
            "folded": (bool(self._safe_attr(track, "fold_state", False))
                       if is_group else None),
            "visible": self._safe_attr(track, "is_visible"),
            "mute": self._safe_attr(track, "mute"),
            "solo": self._safe_attr(track, "solo"),
            "muted_via_solo": self._safe_attr(track, "muted_via_solo"),
            "arm": (bool(self._safe_attr(track, "arm", False))
                    if armable else None),
            "implicit_arm": self._safe_attr(track, "implicit_arm"),
            "can_be_armed": armable,
            "input": self._routing(track, "input"),
            "monitoring": self._monitoring_name(track),
            "output": self._routing(track, "output"),
            "device_count": len(list(self._safe_attr(track, "devices", [])
                                     or [])),
            "arrangement_clip_count": self._arrangement_clip_count(track),
        }

    def _list_tracks(self, include_returns=True, include_master=True):
        """One compact row per track, in a single pass."""
        try:
            rows = [self._track_row(t, i)
                    for i, t in enumerate(self._song.tracks)]
            if include_returns:
                for i, track in enumerate(self._song.return_tracks):
                    row = self._track_row(track, None)
                    row["return_track"] = self._return_track_label(i)
                    rows.append(row)
            if include_master:
                rows.append(self._track_row(self._song.master_track, None))
            return {"tracks": rows}
        except Exception as e:
            self.log_message("Error listing tracks: " + str(e))
            raise

    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise
    
    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise
    
    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise
    
    
    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise

    # Browser helper methods

    def _normalize_browser_category_name(self, category_name):
        """Normalize browser category names for robust matching."""
        try:
            normalized = (category_name or "").strip().lower()
            normalized = normalized.replace("-", "_").replace(" ", "_")
            while "__" in normalized:
                normalized = normalized.replace("__", "_")
            normalized = normalized.strip("_")

            # Canonical category aliases
            alias_map = {
                "instrument": "instruments",
                "sound": "sounds",
                "drum": "drums",
                "audioeffects": "audio_effects",
                "audio_fx": "audio_effects",
                "audiofx": "audio_effects",
                "midieffects": "midi_effects",
                "midi_fx": "midi_effects",
                "midifx": "midi_effects",
                "plugin": "plugins",
                "vst": "plugins",
                "vst2": "plugins",
                "vst3": "plugins",
                "au": "plugins",
            }

            if normalized in alias_map:
                return alias_map[normalized]

            compact = normalized.replace("_", "")
            if compact in alias_map:
                return alias_map[compact]

            return normalized
        except Exception:
            return (category_name or "").strip().lower()

    def _split_browser_path(self, path):
        """Split a browser path into normalized path parts."""
        if not path:
            return []
        return [part.strip() for part in path.split("/") if part and part.strip()]

    def _resolve_browser_root_category(self, browser, root_category, browser_attrs=None):
        """Resolve a root browser category to the corresponding browser item."""
        normalized_root = self._normalize_browser_category_name(root_category)

        standard_roots = {
            "instruments": "instruments",
            "sounds": "sounds",
            "drums": "drums",
            "audio_effects": "audio_effects",
            "midi_effects": "midi_effects",
            "plugins": "plugins",
        }

        attr_name = standard_roots.get(normalized_root)
        if attr_name and hasattr(browser, attr_name):
            return getattr(browser, attr_name), attr_name

        attrs = browser_attrs if browser_attrs is not None else [a for a in dir(browser) if not a.startswith('_')]
        for attr in attrs:
            if self._normalize_browser_category_name(attr) == normalized_root:
                try:
                    return getattr(browser, attr), attr
                except Exception as e:
                    self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))

        return None, normalized_root
    
    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = self._split_browser_path(path)
                if not path_parts:
                    result["error"] = "Invalid path"
                    return result
                
                # Determine the root based on the first part
                current_item, resolved_attr = self._resolve_browser_root_category(app.browser, path_parts[0])
                if current_item is None:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                elif resolved_attr != "instruments":
                    # Keep path parts aligned with resolved root
                    path_parts[0] = resolved_attr
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI"""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item
            
            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None
            
            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                # Check all main categories
                categories = [
                    browser_or_item.instruments,
                    browser_or_item.sounds,
                    browser_or_item.drums,
                    browser_or_item.audio_effects,
                    browser_or_item.midi_effects,
                    browser_or_item.plugins,
                ]
                
                for category in categories:
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item
                
                return None
            
            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item
            
            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Arrangement command handlers

    def _get_arrangement_info(self, track_index):
        """Get arrangement clips and transport for one or all tracks."""
        try:
            transport = self._get_transport_info()
            tracks_data = []

            if track_index == -1:
                tracks = list(enumerate(self._song.tracks))
            else:
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                tracks = [(track_index, self._song.tracks[track_index])]

            for idx, track in tracks:
                is_group = bool(getattr(track, "is_foldable", False))
                if is_group and track_index == -1:
                    continue

                clips = []
                if not is_group:
                    for ci, clip in enumerate(track.arrangement_clips):
                        clip_info = self._get_arrangement_clip_info(clip)
                        clip_info["index"] = ci
                        clips.append(clip_info)

                tracks_data.append({
                    "index": idx,
                    "name": track.name,
                    "is_midi": track.has_midi_input,
                    "is_audio": track.has_audio_input,
                    "is_group_track": is_group,
                    "arrangement_clips": clips,
                    "clip_count": len(clips),
                })

            return {"transport": transport, "tracks": tracks_data}
        except Exception as e:
            self.log_message("Error getting arrangement info: " + str(e))
            raise

    def _get_cue_points(self):
        """Get all cue points."""
        try:
            cue_points = []
            for cp in self._song.cue_points:
                cue_points.append({"name": cp.name, "time": cp.time})
            return {"cue_points": cue_points}
        except Exception as e:
            self.log_message("Error getting cue points: " + str(e))
            raise

    def _set_song_time(self, time):
        """Set playback position."""
        try:
            self._song.current_song_time = time
            return {"time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting song time: " + str(e))
            raise

    def _set_arrangement_loop(self, enabled, start=None, length=None):
        """Set arrangement loop state and region."""
        try:
            self._song.loop = enabled
            if start is not None:
                self._song.loop_start = start
            if length is not None:
                self._song.loop_length = length
            return {
                "enabled": self._song.loop,
                "start": self._song.loop_start,
                "length": self._song.loop_length,
            }
        except Exception as e:
            self.log_message("Error setting arrangement loop: " + str(e))
            raise

    def _jump_to_cue(self, direction=None, name=None):
        """Jump to cue point by direction or name."""
        try:
            if direction == "next":
                self._song.jump_to_next_cue()
                return {"direction": "next", "time": self._song.current_song_time}
            elif direction == "prev":
                self._song.jump_to_prev_cue()
                return {"direction": "prev", "time": self._song.current_song_time}
            elif name:
                for cp in self._song.cue_points:
                    if cp.name == name:
                        cp.jump()
                        return {"name": cp.name, "time": cp.time}
                raise ValueError("Cue point '{0}' not found".format(name))
            else:
                raise ValueError("Provide direction ('next'/'prev') or name")
        except Exception as e:
            self.log_message("Error jumping to cue: " + str(e))
            raise

    def _create_cue_point(self, time, name=""):
        """Create a cue point at the given time."""
        try:
            for cp in tuple(self._song.cue_points):
                if abs(cp.time - time) < 0.01:
                    raise ValueError("Cue point already exists at this position: " + cp.name)
            self._song.current_song_time = time
            self._song.set_or_delete_cue()
            if name:
                for cp in tuple(self._song.cue_points):
                    if abs(cp.time - time) < 0.01:
                        cp.name = name
                        break
            return {"time": time, "name": name}
        except Exception as e:
            self.log_message("Error creating cue point: " + str(e))
            raise

    def _delete_cue_point(self, time):
        """Delete a cue point at the given time."""
        try:
            found = False
            for cp in self._song.cue_points:
                if abs(cp.time - time) < 0.01:
                    found = True
                    break
            if not found:
                raise ValueError("No cue point at this position")
            self._song.current_song_time = time
            self._song.set_or_delete_cue()
            return {"deleted": True}
        except Exception as e:
            self.log_message("Error deleting cue point: " + str(e))
            raise

    def _validate_not_return_or_master(self, track_index):
        """Raise if track is a return or master track."""
        track = self._song.tracks[track_index]
        # Return tracks and master track are separate in the Live API,
        # but if accessed via tracks list they are regular tracks.
        # Check by comparing against return_tracks and master_track.
        for rt in self._song.return_tracks:
            if track == rt:
                raise ValueError("Cannot create arrangement clips on return track '{0}'".format(track.name))
        if track == self._song.master_track:
            raise ValueError("Cannot create arrangement clips on master track")

    def _create_arrangement_clip(self, track_index, position, length):
        """Create MIDI clip in arrangement."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            self._validate_not_return_or_master(track_index)
            track = self._song.tracks[track_index]
            overlapped = self._check_overlap(track, position, length)
            track.create_midi_clip(position, length)
            # Find the newly created clip
            result = {"start_time": position, "length": length, "is_midi": True}
            for clip in track.arrangement_clips:
                if abs(clip.start_time - position) < 0.01:
                    result = self._get_arrangement_clip_info(clip)
                    break
            result["overlapped_clips"] = overlapped
            return result
        except Exception as e:
            self.log_message("Error creating arrangement clip: " + str(e))
            raise

    def _create_arrangement_audio_clip(self, track_index, position, file_path):
        """Create audio clip in arrangement from file."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            self._validate_not_return_or_master(track_index)
            track = self._song.tracks[track_index]
            track.create_audio_clip(file_path, position)
            # Find the newly created clip
            result = {"start_time": position, "file_path": file_path, "is_audio": True}
            for clip in track.arrangement_clips:
                if abs(clip.start_time - position) < 0.01:
                    result = self._get_arrangement_clip_info(clip)
                    break
            return result
        except Exception as e:
            self.log_message("Error creating arrangement audio clip: " + str(e))
            raise

    def _duplicate_to_arrangement(self, track_index, clip_index, destination_time):
        """Duplicate a session clip to arrangement."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip in slot {0}".format(clip_index))
            clip = slot.clip
            track.duplicate_clip_to_arrangement(clip, destination_time)
            # Find the duplicated clip
            result = {"start_time": destination_time, "duplicated": True}
            for arr_clip in track.arrangement_clips:
                if abs(arr_clip.start_time - destination_time) < 0.01:
                    result = self._get_arrangement_clip_info(arr_clip)
                    result["duplicated"] = True
                    break
            return result
        except Exception as e:
            self.log_message("Error duplicating to arrangement: " + str(e))
            raise

    def _add_notes_to_arrangement_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to an arrangement clip."""
        try:
            track, clip = self._resolve_arrangement_clip(track_index, clip_index)
            if not clip.is_midi_clip:
                raise ValueError("Clip is not a MIDI clip")
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                live_notes.append((pitch, start_time, duration, velocity, mute))
            clip.set_notes(tuple(live_notes))
            return {"note_count": len(notes)}
        except Exception as e:
            self.log_message("Error adding notes to arrangement clip: " + str(e))
            raise

    def _delete_arrangement_clip(self, track_index, clip_index=None, clip_name=None):
        """Delete an arrangement clip."""
        try:
            track, clip = self._resolve_arrangement_clip(track_index, clip_index, clip_name)
            track.delete_clip(clip)
            return {"deleted": True}
        except Exception as e:
            self.log_message("Error deleting arrangement clip: " + str(e))
            raise

    def _set_arrangement_clip_property(self, track_index, clip_index, property_name, value,
                                       clip_name=None):
        """Set a property on an arrangement clip addressed by index or name.

        The returned clip_name is the clip's name before the change, so the
        caller can report which clip was actually modified.
        """
        try:
            ALLOWED = ("name", "muted", "color", "looping", "loop_start", "loop_end",
                       "gain", "pitch_coarse", "pitch_fine", "warping", "warp_mode")
            if property_name not in ALLOWED:
                raise ValueError("Property '{0}' not allowed. Allowed: {1}".format(
                    property_name, ", ".join(ALLOWED)))
            track, clip = self._resolve_arrangement_clip(track_index, clip_index, clip_name)
            resolved_name = clip.name
            setattr(clip, property_name, value)
            return {"clip_name": resolved_name, "property": property_name,
                    "value": getattr(clip, property_name)}
        except Exception as e:
            self.log_message("Error setting arrangement clip property: " + str(e))
            raise

    def _set_view(self, view_name):
        """Switch Ableton view."""
        try:
            app = self.application()
            app.view.show_view(view_name)
            return {"visible": app.view.is_view_visible(view_name)}
        except Exception as e:
            self.log_message("Error setting view: " + str(e))
            raise

    def _control_arrangement_view(self, action, track_index=0):
        """Dispatch arrangement view control actions."""
        try:
            app = self.application()
            if action == "zoom_in":
                app.view.zoom_view(1, "Arranger", False)
            elif action == "zoom_out":
                app.view.zoom_view(0, "Arranger", False)
            elif action == "scroll_right":
                app.view.scroll_view(1, "Arranger", False)
            elif action == "scroll_left":
                app.view.scroll_view(0, "Arranger", False)
            elif action == "follow_on":
                self._song.view.follow_song = True
            elif action == "follow_off":
                self._song.view.follow_song = False
            elif action == "collapse_track":
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                self._song.tracks[track_index].view.is_collapsed = True
            elif action == "expand_track":
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                self._song.tracks[track_index].view.is_collapsed = False
            else:
                raise ValueError("Unknown action: " + action)
            return {"action": action, "done": True}
        except Exception as e:
            self.log_message("Error controlling arrangement view: " + str(e))
            raise

    def _manage_clip_automation(self, track_index, clip_index, action, parameter_name="",
                                clip_name=None):
        """Create or clear automation envelopes on a clip addressed by index or name."""
        try:
            track, clip = self._resolve_arrangement_clip(track_index, clip_index, clip_name)
            if action == "clear_all":
                clip.clear_all_envelopes()
                return {"action": "clear_all", "clip_name": clip.name, "done": True}
            # Find the parameter
            param = None
            # Check mixer device first
            mixer = track.mixer_device
            for p in [mixer.volume, mixer.panning]:
                if p.name.lower() == parameter_name.lower():
                    param = p
                    break
            # Check sends
            if param is None:
                for send in mixer.sends:
                    if send.name.lower() == parameter_name.lower():
                        param = send
                        break
            # Check track devices
            if param is None:
                for device in track.devices:
                    for p in device.parameters:
                        if p.name.lower() == parameter_name.lower():
                            param = p
                            break
                    if param:
                        break
            if param is None:
                raise ValueError("Parameter '{0}' not found on track '{1}'".format(
                    parameter_name, track.name))
            if action == "create":
                clip.create_automation_envelope(param)
                return {"action": "create", "parameter": param.name,
                        "clip_name": clip.name, "done": True}
            elif action == "clear":
                clip.clear_envelope(param)
                return {"action": "clear", "parameter": param.name,
                        "clip_name": clip.name, "done": True}
            else:
                raise ValueError("Unknown action: " + action)
        except Exception as e:
            self.log_message("Error managing clip automation: " + str(e))
            raise

    # ── Device command handlers ──────────────────────────────────────

    def _get_device_parameters(self, track_index, device_index, chain_index=None, show_all=False):
        """Return parameter list for a device."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            # If chain_index, navigate into chain
            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chains = device.chains
                if chain_index < 0 or chain_index >= len(chains):
                    raise IndexError("Chain index {0} out of range".format(chain_index))
                chain = chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain '{0}' has no devices".format(chain.name))
                target_device = chain.devices[0]

            params = target_device.parameters
            param_list = []
            for i, p in enumerate(params):
                pmin = p.min
                pmax = p.max
                raw_val = p.value
                norm = (raw_val - pmin) / (pmax - pmin) if pmax != pmin else 0.0
                entry = {
                    "index": i,
                    "name": p.name,
                    "value": round(norm, 4),
                    "min": pmin,
                    "max": pmax,
                    "display_value": str(p),
                    "is_enabled": p.is_enabled,
                    "is_quantized": p.is_quantized,
                    "value_items": list(p.value_items) if p.is_quantized else [],
                }
                param_list.append(entry)

            result = {
                "device_name": target_device.name,
                "device_class": target_device.class_name,
                "parameter_count": len(params),
                "parameters": param_list,
            }
            return result
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, chain_index=None,
                              parameter_name=None, parameter_index=None, value=0.0):
        """Set a device parameter by name or index. Value is normalized 0.0-1.0."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chain = device.chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain '{0}' has no devices".format(chain.name))
                target_device = chain.devices[0]

            param, match_type = self._find_parameter(
                target_device, name=parameter_name, index=parameter_index)

            if not param.is_enabled:
                raise ValueError("Parameter '{0}' is currently disabled".format(param.name))

            pmin = param.min
            pmax = param.max
            old_norm = (param.value - pmin) / (pmax - pmin) if pmax != pmin else 0.0

            # Clamp normalized value
            clamped = max(0.0, min(1.0, value))
            was_clamped = (clamped != value)

            # Denormalize
            raw_value = pmin + clamped * (pmax - pmin)
            param.value = raw_value

            return {
                "parameter_name": param.name,
                "old_value": round(old_norm, 4),
                "new_value": round(clamped, 4),
                "display_value": str(param),
                "clamped": was_clamped,
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _set_device_enabled(self, track_index, device_index, chain_index=None, enabled=True):
        """Enable or disable a device via its 'Device On' parameter."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chain = device.chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain has no devices")
                target_device = chain.devices[0]

            # Use "Device On" parameter (always index 0) instead of
            # is_active which is read-only in the Live API.
            params = target_device.parameters
            if not params:
                raise ValueError("Device '{0}' has no parameters".format(target_device.name))
            on_param = params[0]
            on_param.value = on_param.max if enabled else on_param.min

            return {
                "device_name": target_device.name,
                "is_active": enabled,
            }
        except Exception as e:
            self.log_message("Error setting device enabled: " + str(e))
            raise

    def _get_chain_info(self, track_index, device_index, chain_index=None):
        """Get chain information for a rack device."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            if not device.can_have_chains:
                raise ValueError("Device '{0}' is not a rack and has no chains".format(device.name))

            if chain_index is not None:
                # Detail for a specific chain
                chains = device.chains
                if chain_index < 0 or chain_index >= len(chains):
                    raise IndexError("Chain index {0} out of range".format(chain_index))
                chain = chains[chain_index]
                devices = []
                for di, d in enumerate(chain.devices):
                    devices.append({
                        "index": di,
                        "name": d.name,
                        "class_name": d.class_name,
                        "type": self._get_device_type(d),
                        "is_active": d.is_active,
                        "parameter_count": len(d.parameters),
                    })
                return {
                    "chain_name": chain.name,
                    "devices": devices,
                }
            else:
                # List all chains
                chain_list = []
                for ci, chain in enumerate(device.chains):
                    chain_devices = []
                    for di, d in enumerate(chain.devices):
                        chain_devices.append({
                            "index": di,
                            "name": d.name,
                            "type": self._get_device_type(d),
                        })
                    chain_list.append({
                        "index": ci,
                        "name": chain.name,
                        "mute": chain.mute,
                        "solo": chain.solo,
                        "device_count": len(chain.devices),
                        "devices": chain_devices,
                    })
                return {
                    "device_name": device.name,
                    "chain_count": len(device.chains),
                    "chains": chain_list,
                }
        except Exception as e:
            self.log_message("Error getting chain info: " + str(e))
            raise

    def _get_drum_pad_info(self, track_index, device_index):
        """Get drum pad info for a Drum Rack."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            if not device.can_have_drum_pads:
                raise ValueError("Device '{0}' is not a Drum Rack".format(device.name))

            filled_pads = []
            for pad in device.drum_pads:
                if pad.chains:
                    pad_devices = []
                    for chain in pad.chains:
                        for d in chain.devices:
                            pad_devices.append({
                                "index": 0,
                                "name": d.name,
                                "type": self._get_device_type(d),
                            })
                    filled_pads.append({
                        "note": pad.note,
                        "name": pad.name,
                        "mute": pad.mute,
                        "solo": pad.solo,
                        "chains": [{
                            "name": c.name,
                            "devices": [{"index": di, "name": d.name, "type": self._get_device_type(d)}
                                        for di, d in enumerate(c.devices)]
                        } for c in pad.chains],
                    })

            return {
                "device_name": device.name,
                "filled_pads": filled_pads,
            }
        except Exception as e:
            self.log_message("Error getting drum pad info: " + str(e))
            raise

    def _delete_device(self, track_index, device_index):
        """Delete a device from a track."""
        try:
            track, device = self._resolve_device(track_index, device_index)
            device_name = device.name
            track.delete_device(device_index)
            return {
                "deleted_device": device_name,
                "remaining_devices": len(track.devices),
            }
        except Exception as e:
            self.log_message("Error deleting device: " + str(e))
            raise

    def _delete_track(self, track_index):
        """Delete a track from the session."""
        try:
            if len(self._song.tracks) <= 1:
                raise ValueError(
                    "Cannot delete the last remaining session track. "
                    "Ableton must always have at least one track."
                )
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(self._song.tracks) - 1))
            track_name = self._song.tracks[track_index].name
            self._song.delete_track(track_index)
            return {
                "deleted_track": track_name,
                "remaining_tracks": len(self._song.tracks),
            }
        except Exception as e:
            self.log_message("Error deleting track: " + str(e))
            raise

    def _navigate_preset(self, track_index, device_index, chain_index=None, direction="current"):
        """Navigate device presets."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chain = device.chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain has no devices")
                target_device = chain.devices[0]

            if not hasattr(target_device, 'presets') or not target_device.presets:
                raise ValueError("Device '{0}' has no presets available".format(
                    target_device.name))

            presets = list(target_device.presets)
            current_idx = target_device.selected_preset_index

            if direction == "next":
                new_idx = min(current_idx + 1, len(presets) - 1)
                target_device.selected_preset_index = new_idx
            elif direction == "previous":
                new_idx = max(current_idx - 1, 0)
                target_device.selected_preset_index = new_idx
            elif direction == "current":
                new_idx = current_idx
            else:
                raise ValueError("Invalid direction: {0}".format(direction))

            return {
                "device_name": target_device.name,
                "preset_name": presets[new_idx] if new_idx < len(presets) else "",
                "preset_index": new_idx,
                "preset_count": len(presets),
            }
        except Exception as e:
            self.log_message("Error navigating preset: " + str(e))
            raise

    # Device helper methods

    def _resolve_device(self, track_index, device_index, chain_index=None):
        """Resolve a device reference to (track, device) tuple.

        Parameters:
        - track_index: 0-based track index
        - device_index: 0-based device index on track (or within chain)
        - chain_index: optional 0-based chain index for rack devices

        Returns (track, device) tuple.
        Raises IndexError or ValueError on invalid references.
        """
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index {0} out of range (0-{1})".format(
                track_index, len(self._song.tracks) - 1))

        track = self._song.tracks[track_index]

        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index {0} out of range on track '{1}' (0-{2})".format(
                device_index, track.name,
                len(track.devices) - 1 if track.devices else 0))

        device = track.devices[device_index]

        if chain_index is not None:
            # Navigate into rack chain
            if not device.can_have_chains:
                raise ValueError("Device '{0}' is not a rack and has no chains".format(device.name))
            chains = device.chains
            if chain_index < 0 or chain_index >= len(chains):
                raise IndexError("Chain index {0} out of range on '{1}' (0-{2})".format(
                    chain_index, device.name, len(chains) - 1))
            # Return the first device in the chain (or the chain's device list)
            # For now, return the chain's first device. If a nested device_index
            # is needed, callers can extend this.
            chain = chains[chain_index]
            if not chain.devices:
                raise ValueError("Chain '{0}' has no devices".format(chain.name))
            # Use device_index 0 within the chain for now (callers pass separate index)
            return track, device

        return track, device

    def _find_parameter(self, device, name=None, index=None):
        """Find a parameter on a device by name or index.

        Lookup order:
        1. Exact name match (case-sensitive)
        2. Case-insensitive exact match
        3. Case-insensitive partial match
        4. By 0-based index

        Returns (parameter, match_type) tuple where match_type is
        'exact', 'case_insensitive', 'partial', or 'index'.
        Raises ValueError if not found.
        """
        params = device.parameters

        if name is not None:
            # Step 1: exact match
            for p in params:
                if p.name == name:
                    return p, "exact"

            # Step 2: case-insensitive exact match
            name_lower = name.lower()
            for p in params:
                if p.name.lower() == name_lower:
                    return p, "case_insensitive"

            # Step 3: case-insensitive partial match
            for p in params:
                if name_lower in p.name.lower():
                    return p, "partial"

            raise ValueError("Parameter '{0}' not found on device '{1}'".format(
                name, device.name))

        if index is not None:
            if index < 0 or index >= len(params):
                raise IndexError("Parameter index {0} out of range on '{1}' (0-{2})".format(
                    index, device.name, len(params) - 1))
            return params[index], "index"

        raise ValueError("Either name or index must be provided")

    def _get_device_info(self, device):
        """Build a device info dict."""
        info = {
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device),
            "is_active": device.is_active,
            "can_have_chains": device.can_have_chains,
            "can_have_drum_pads": device.can_have_drum_pads,
            "parameter_count": len(device.parameters),
        }
        try:
            info["class_display_name"] = device.class_display_name
        except Exception:
            info["class_display_name"] = device.class_name
        return info

    # General helper methods

    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"
    
    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # Helper function to process a browser item and its children
            def process_item(item, depth=0):
                if not item:
                    return None
                
                result = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": hasattr(item, 'children') and bool(item.children),
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }
                
                
                return result
            
            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = self._split_browser_path(path)
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0]
            current_item, resolved_root = self._resolve_browser_root_category(
                app.browser, root_category, browser_attrs
            )
            if current_item is None:
                # If we still haven't found the category, return available categories
                return {
                    "path": path,
                    "error": "Unknown or unavailable category: {0}".format(
                        self._normalize_browser_category_name(root_category)
                    ),
                    "available_categories": browser_attrs,
                    "items": []
                }

            # Keep path canonicalized to resolved root category
            path_parts[0] = resolved_root
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
