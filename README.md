# NIU Home Assistant integration

This custom component integrates compatible NIU vehicles with Home Assistant.

Now this integration is _asynchronous_ and it is easy installable via config flow.

## Changes:
* It automatically creates a NIU device so its sensors and camera are grouped together.
![auto device](images/niu_integration_device.png)
* If you select the Last track sensor automatically it will create a camera integration, with the rendered image of your last track.
![last track camera](images/niu_integration_camera.png)

With the thanks to pikka97 !!!

## Setup
1. In Home Assistant's settings under "device and services" click on the "Add integration" button.
2. Search for "NIU" and click on it.
3. Enter your NIU account credentials and select the sensors you want to add.
![config flow](images/config_flow_niu_integration.png)
4. Enjoy your new NIU integration :-)

## Known bugs

some people had problems with this version please take the latest 1.o  versions
See https://github.com/marcelwestrahome/home-assistant-niu-component repository

## Local GPS trip entities

Existing config entries automatically gain these entities after reloading the
integration. The selected battery, vehicle and camera entities keep their names
and unique IDs. The new entities use the same scooter prefix and serial number;
for example, a scooter with prefix `MQi` normally gets
`sensor.niu_scooter_mqi_last_trip`. Home Assistant may add a suffix on a name
collision, and entity IDs can be renamed in the entity registry.

| Name suffix | State | Attributes / behavior |
| --- | --- | --- |
| Last Trip | Latest downloaded trip ID | Metadata and `route` coordinates |
| Trip History | Number of downloaded trips | Recent metadata in `trips`, without GPS points |
| Trip Count | Number of downloaded trips | Excludes initial-sync skip markers |
| Trip (select) | Selected trip ID | All retained downloaded trip IDs, newest ride first |
| Selected Trip | Selected trip ID | Metadata and `route` coordinates for that ride |

`route` is an ordered list of `{ "latitude": 52.0, "longitude": 5.0 }` objects.
`gps_points_count` reports the original API point count,
`valid_gps_points_count` reports the usable coordinates, and
`route_points_count` reports the displayed points. Metadata includes `track_id`,
`date`, `start_time` / `end_time` (NIU Unix milliseconds), `distance_km` and
`duration_seconds` when supplied by NIU. Summary `distance` is converted from
meters to kilometers; detail `mileage` is already in kilometers.

Routes with at most 200 valid points are exposed in full. Larger routes are
sampled evenly, retaining the start and end points; `route_truncated` indicates
this. Each route or metadata index payload is limited to 12 KiB. `trips` contains
up to 50 recent records and `trips_truncated` indicates omitted records; the
selector still offers every downloaded trip retained in local storage.

The full original GPS details remain in Home Assistant's existing
`.storage/niu_trip_history_<entry_id>` file. The full archive is retained locally;
the former 500-record pruning limit is removed. `route`, `trips`, and the select's `options` are excluded
from Recorder history, so these large lists are available through the **current
state API**, rather than copied into Recorder for every state change. Recorder
still stores the short states and scalar metadata.

Choose an ID from the Trip selector with `select.select_option` to update
Selected Trip immediately. Selection does not request NIU data and is saved
across restarts. Before a choice is made, the selector follows the newest saved
ride. An explicit historical choice remains selected when newer rides arrive;
if that ride is evicted, selection falls back to the newest saved ride. With no
saved rides the route sensors are unknown, the count is zero and the selector
has no options. Locally stored routes remain available during cloud outages.

The coordinator polls ordinary scooter data every 150 seconds. While history
work remains, it runs a separate history step every 30 seconds without polling
battery/vehicle endpoints more often. Each step reads at most one older metadata
page of 50 rides and downloads at most one GPS route. NIU's first-page array and
older-page `data.items` envelopes are both supported. Discovery stops only at an
empty page; duplicate pages or malformed responses pause with an error instead
of falsely declaring completion. New rides take priority over older routes.

The metadata index, next page, downloaded routes and retry deadlines are stored
locally, so a restart resumes the queue. The complete index is rescanned daily
to catch rides missed during an extended offline period. Downloaded GPS routes
are never fetched again. Existing initial-sync skip markers become eligible for
download on upgrade. Once the queue is processed, polling returns to 150 seconds.

NIU exposes the GPS detail endpoint for one ride at a time; no GPS bulk endpoint
is used. All routes are fetched sequentially. A server rate limit pauses history
requests for at least the returned Retry-After duration. Transport/authentication
errors pause history for five minutes without classifying routes as unavailable.
If a successful response has no GPS data, it is checked up to three times with
five- and ten-minute retry delays, then retained as metadata with `gps_unavailable`
in local storage. Such records do not inflate the GPS trip count or selector.

Trip History exposes scalar progress alongside its bounded recent-route index:
`discovered_trips`, `pending_trips`, `unavailable_gps_trips`,
`history_scan_complete`, `next_history_page`, `sync_status` and `sync_error`.
`sync_status` is `discovering`, `downloading`, `waiting`, `complete`, or
`complete_with_missing_gps`. Its state and `total_trips` still count downloaded
GPS rides; the selector offers every downloaded route in the full local archive.

## Raw riding mode

The connection sensor also exposes `gps_timestamp_ms`, `info_timestamp_ms`,
`is_acc_on` and `current_speed` from the same cached vehicle response as its
coordinates. Timestamps are NIU Unix milliseconds, not the time Home Assistant
polled the API. GPS may be older than status; missing fields remain null. Use
the GPS timestamp for position age and the info timestamp for speed/ACC age.
Cloud measurements and reachability estimates do not establish current motion
or travel direction. These attributes add no API requests.

The **Refresh Status** button explicitly reads only
`/v5/scooter/motor_data/index_info`, bypassing the normal 150-second polling
interval. It publishes the returned ACC, speed and position to the existing
sensors without sending scooter commands or downloading trips. The connection
sensor's `status_checked_at` attribute is the successful explicit request time
(Unix seconds); it does not replace NIU's measurement timestamps or guarantee
that the scooter has supplied a new measurement. Failed requests leave that
marker unchanged and report an error. Explicit and scheduled reads are
serialized and respect API rate-limit backoff.

The **RidingMode** sensor exposes the provider's `cycling_model` value unchanged.
It is added automatically to existing entries and uses the normal shared status
polling, without extra API calls. The numeric codes have not been mapped to
E-Save, Wet, Dynamic, Sports or Master: verify them against your own scooter.
A missing field is reported as unknown. This sensor reads cloud status; it does
not send commands or represent individual handlebar button presses.

## Cloud controls

Existing entries gain these controls on the existing NIU device without changing
sensor IDs, credentials or stored trip history:

| Name | Entity | Behavior |
| --- | --- | --- |
| Open Seat | button | Sends the app's `cushion_lock_on` command to release the seat/storage compartment |
| Auto Sleep | switch | Reads/writes `low_power_mode_value`; this is the Auto Sleep setting, not current sleep or low-battery E-Save status |
| Charging Limit | select | Charge ceiling: 80%, 85%, 90%, 95% or 100% |
| Charging Power | number | Whole watts within the scooter's reported `charge_power_range` |
| Regenerative Braking | select | Uit (0), Niveau 3, Standaard (5), Niveau 7, Sterk (10) |
| Traction Control | switch | TCS on/off |
| Auto Hold | switch | `auto_parking_value` on/off, using the app's Auto Hold meaning |
| Auto-Off | select | Provider-reported durations (5/10/30 minutes, 1/8 hours on supported vehicles) |
| Volume | number | Whole sound levels from 0 to the reported `sup_sound_volume_max` |
| Sound Theme | select | Provider-reported themes, e.g. Standard / Techno / Gaming |
| Wake / Power Off | buttons | ACC electronics on/off; do not represent motor motion |
| Arm Alarm / Disarm Alarm | buttons | Anti-theft fortification commands; do not represent a mechanical lock |

Vehicle settings, sound settings and Auto-Off use a separate coordinator with a
150-second poll interval. Each source has its own availability state. Settings
failures do not interrupt existing battery/status sensors or GPS backfill.
Unsupported settings are unavailable; a subsequent successful poll recovers
availability. Charging Power remains unavailable when NIU omits valid limits
(e.g. while unplugged). When available, its `reported_setting_w` attribute exposes the
reported value; HA omits extra attributes while the entity is unavailable. No charger/model-specific minimum or maximum is guessed.

Each settings write sends only the selected field plus `sn` and cloud transport
`db_cmd_type: "1"`, then reads provider settings back. Vehicle fields use
`/v5/scooter/car_machine/align`; Volume and Sound Theme use
`/v5/scooter/sound/theme`; Auto-Off uses
`/v5/users_bind/safety/auto_close_setting`. Auto-Off sends `auto_off_value` as
integer seconds and reads `auto_off_time_value`; its choices come from the
provider's `list`. The observed app requests send the other setting values as
strings. Do not substitute `auto_off_duration` from the align response. Entities never assume the requested value was applied; cloud propagation
may take until a later poll. API failures are reported by the action. No commands
are sent during setup, polling or restart. Requests that time out are not retried.

Open Seat sends JSON to `/v5/cmd/creat` with `type: "cushion_lock_on"` and the
authentication token in the header, matching an observed official app request.
It requests opening, rather than claiming the seat's physical state. The app
also queries `/v5/motor/cmdstatus`; its response semantics have not yet been
verified, so this integration does not treat a button press as proof of opening.
Compatibility depends on the scooter, firmware and cloud connection.

Regenerative Braking uses `foc_ebs_level_value` and `sup_foc_ebs_level`, rather
than guessing the meaning of the separate energy-recovery fields. The precise
app names for levels 3 and 7 have not been established, so the select uses neutral
numeric labels. Auto Hold uses `sup_auto_parking`; TCS uses `sup_tcs`.

The additional command buttons use `/v5/cmd/creat` with `acc_on`, `acc_off`,
`fortification_on` and `fortification_off`. These command names and the sound
write fields are also implemented by the
[NeariX integration](https://github.com/NeariX67/home-assistant-niu-component).
They are explicit requests, without optimistic lock/power state. Physical
execution and compatibility have not been automatically tested on every model.
