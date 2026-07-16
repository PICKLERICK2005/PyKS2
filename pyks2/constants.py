"""Constants for the K-S2 WiFi API.

Endpoint templates, defaults, and the enum value sets the camera reports in its
``constants/camera`` response. These mirror the hardware-verified API map in
``examples/API_REFERENCE.json``.

Note on enum lists: the camera is the source of truth at runtime (fetch
``constants/camera``), and some lists — notably ``avList`` — are *dynamic* and
change with lens/aperture state (see PROTOCOL.md §4). The lists here are the
statically-observed defaults, useful for offline validation and documentation,
but a live client should prefer what the camera reports.
"""

from __future__ import annotations

# --- connection defaults ---------------------------------------------------

DEFAULT_IP = "192.168.0.1"
DEFAULT_PORT = 80
DEFAULT_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 90.0  # full DNG over WiFi is ~55s; give headroom

# The camera server is most reliable one-request-per-socket.
DEFAULT_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Connection": "close",
    "Accept-Encoding": "identity",
}

# --- endpoint templates ----------------------------------------------------
# {ip} is filled by the client; {sub}/{dir}/{file} by callers.

class EP:
    """Endpoint path templates (relative; the client prepends http://{ip})."""
    APIS = "/v1/apis"
    PING = "/v1/ping"
    PROPS = "/v1/props"
    PROPS_SUB = "/v1/props/{sub}"
    CONSTANTS_SUB = "/v1/constants/{sub}"
    PARAMS_SUB = "/v1/params/{sub}"
    VARIABLES_SUB = "/v1/variables/{sub}"
    STATUS_SUB = "/v1/status/{sub}"

    SHOOT = "/v1/camera/shoot"
    SHOOT_START = "/v1/camera/shoot/start"   # BULB open (dial=B); 412 otherwise
    SHOOT_FINISH = "/v1/camera/shoot/finish"  # BULB close (dial=B); 412 otherwise
    FOCUS = "/v1/lens/focus"

    PHOTOS = "/v1/photos"
    PHOTO_INFO = "/v1/photos/{dir}/{file}/info"
    PHOTO_LATEST_INFO = "/v1/photos/latest/info"
    PHOTO_FILE = "/v1/photos/{dir}/{file}"

    LIVEVIEW = "/v1/liveview"
    LIVEVIEW_ZOOM = "/v1/liveview/zoom"
    CHANGES = "/v1/changes"  # WebSocket upgrade


# --- subsystems / facets ---------------------------------------------------

SUBSYSTEMS = ("camera", "lens", "liveview", "device")
READ_GROUPS = ("constants", "params", "variables", "status", "props")

# --- capability enums (statically observed on firmware 01.10) --------------
# Prefer the live constants/camera response; these are for offline reference.

WB_MODES = (
    "auto", "multiAuto", "daylight", "shade", "cloud", "daylightFluorescent",
    "dayWhiteFluorescent", "coolWhiteFluorescent", "warmWhiteFluorescent",
    "tungsten", "flash", "cte", "manual1", "colorTemp1",
)

EXPOSURE_MODES = (
    "P", "SV", "TV", "AV", "TAV", "M", "B", "U1", "U2",
    "AHDR", "scene", "autopict", "gps", "movie",
)

SHOOT_MODES = (
    "single", "continuousH", "continuousL", "self12s", "self2s",
    "selfCotinuousH", "selfCotinuousL", "remocon", "remocon3s",
    "remoconContinousH", "remoconContinousL", "bracket", "bracketSelf",
    "bracketRemocon", "multiExp", "multiExpContinuousH", "multiExpContinuousL",
    "multiExpSelf12s", "multiExpSelf2s", "multiExpRemocon", "multiExpRemocon3s",
    "interval", "intervalSelf12s", "intervalSelf2s", "intervalRemocon",
    "intervalRemocon3s", "intervalComp", "intervalCompSelf12s",
    "intervalCompSelf2s", "intervalCompRemocon", "intervalCompRemocon3s",
)

EFFECTS = (
    "cim_natural", "cim_bright", "cim_portrait", "cim_landscape", "cim_vibrant",
    "cim_radiant", "cim_muted", "cim_bleachBypass", "cim_reversal",
    "cim_monochrome", "cim_crossProcess",
)

FILTERS = (
    "off", "dfl_extractColor", "dfl_replaceColor", "dfl_toyCamera", "dfl_retro",
    "dfl_highContrast", "dfl_shading", "dfl_negaPosi", "dfl_solidMonoColor",
    "dfl_hardMonochrome", "hdr_auto", "hdr_mode1", "hdr_mode2", "hdr_mode3",
)

STILL_SIZES = ("L3", "L2", "L1", "M3", "M2", "M1",
               "S3", "S2", "S1", "XS3", "XS2", "XS1")

# --- capture / download options --------------------------------------------

AF_MODES = ("auto", "on", "off")          # POST /v1/camera/shoot body
PHOTO_SIZES = ("view", "full")            # thumb is UNSUPPORTED (400) on K-S2
# view  -> ~54KB JPEG preview
# full  -> ~18MB raw DNG (~55s over WiFi); same as omitting size

# --- event stream ----------------------------------------------------------

# The only two `changed` values the camera emits (PROTOCOL.md §7).
CHANGE_CAMERA = "camera"    # any camera setting changed
CHANGE_STORAGE = "storage"  # a file was written (shot completed)
