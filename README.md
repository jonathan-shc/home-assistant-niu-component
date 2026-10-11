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
`.storage/niu_trip_history_<entry_id>` file (up to 500 retained records, including
initial-sync markers). `route`, `trips`, and the select's `options` are excluded
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

The coordinator now polls the track-list endpoint even for entries which only
selected battery sensors, and processes at most one missing trip per polling
cycle before updating entities. The newest missing route is downloaded first, then historical rides from the
50 most recent trips requested from NIU. Old initial-sync skip markers become
eligible for download, so upgrading fills the history automatically. Already
downloaded routes are preserved and never requested again. Existing retry
backoff is retained. NIU may return fewer than 50 rides or no GPS data for an
older ride; this does not remove previously stored routes. Retention keeps the
500 newest rides by ride time, rather than download time.
