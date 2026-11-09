# eSMART for Home Assistant

Many Swiss apartments come with an **eSMART** wall unit that controls the
lights, blinds and heating. This integration brings those devices into
[Home Assistant](https://www.home-assistant.io/), so you can use them in
automations and dashboards, and pass them on to Apple Home, Google Home or
Alexa through Home Assistant.

> [!IMPORTANT]
> This is a hobby project. It is not made, endorsed or supported by eSMART.
> It works by speaking the same undocumented protocol as the official app,
> and eSMART can change that at any time. Use it at your own risk.

## Features

- Switch and dim your lights.
- Open, close and position your blinds and awnings, including slat tilt.
- Set the heating temperature per room and turn heating on or off.
- Open the entrance door, like the door button in the eSMART app.
- Run the scenes you set up on the wall unit.
- See live power use for the whole apartment, per meter and per device.
- Changes made on the wall unit or in the eSMART app show up in Home
  Assistant right away, and the other way round.

Each room on the wall unit becomes a device in Home Assistant, with the room
name suggested as its area. The door, the scenes and the meters sit on a
separate device called **eSMART Controller**.

## What you need

- Home Assistant **2026.11** or newer.
- An eSMART wall unit with **remote access**, the feature the eSMART app uses
  to connect.
- Internet access. Home Assistant talks to the eSMART cloud, not to the wall
  unit on your local network. If the internet or the eSMART cloud is down,
  so is this integration.

## Installation

eSMART is in the Home Assistant Marketplace catalog.

1. In Home Assistant, go to **Settings → Marketplace** and search for
   **eSMART**.
2. Open it, select **Install**, and restart Home Assistant when it asks you
   to.

Or alternatively the button below opens the eSMART page in the Marketplace of
your Home Assistant directly:

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=b2a3e8&repository=ha-esmart&category=integration)

## Setup

You connect Home Assistant to the wall unit once, the same way you would add
a phone with the eSMART app. The wall unit calls this a "remote".

1. In Home Assistant, go to **Settings → Devices & services → Add
   integration** and search for **eSMART**.
2. On the wall unit, open **Settings → Remote access** and start adding a new
   remote. The wall unit now shows a **pairing code**.
3. Type the code into Home Assistant and submit it right away. The wall unit
   has to accept it within about 45 seconds.

You can also change the name the wall unit lists this remote under. It's
"Home Assistant" unless you change it.

After a few seconds your rooms and devices show up. If you have more than one
eSMART apartment, add each one the same way.

## Using it

Most things work the way you'd expect. A few work differently from other
integrations because of what the wall unit does and doesn't report.

**Lights.** Dimmable lights come back at their last brightness when you turn
them on.

**Blinds and awnings.** In Home Assistant 100 % means fully open and 0 % fully
closed. Blinds with slats also get a tilt control. The wall unit doesn't
report a blind while it moves. When you send a command, Home Assistant shows
the target position straight away. When someone moves a blind at the wall
switch, Home Assistant only sees the new position once it has stopped.
Awnings are reported the same way as blinds, so the integration recognises
them by name: anything with "Markise", "Awning", "Banne" or "Tenda" in its
name becomes an awning.

**Heating.** You can only pick temperatures the wall unit allows, often 18 to
24 °C in whole degrees. Anything in between is rounded to the closest allowed
value. The wall unit tells Home Assistant whether a room is heating right
now, but not whether you switched heating off. Home Assistant remembers what
you set, including across restarts. If you switch heating off somewhere else,
Home Assistant won't notice.

**Door.** The button opens the door. That's all it can do: the doorbell and
the video intercom use a separate channel this integration doesn't support,
so you won't see a ring in Home Assistant.

**Scenes.** Only scenes that are enabled on the wall unit show up. If you
disable one later, it shows as unavailable in Home Assistant. Scenes you
delete on the wall unit disappear.

**Power sensors.** These show live power in watts for the meters (total,
electricity, heating, hot water and so on) and for each light and heating
valve. The wall unit doesn't report energy totals, so the sensors can't be
used in the Energy dashboard directly. To get kWh, add an
[Integral helper](https://www.home-assistant.io/integrations/integration/) on
top of a power sensor and use that instead. The hot water flow rate has no
unit, because the wall unit doesn't say which one it uses.

**Apple Home and others.** To control eSMART devices from Apple Home, use
Home Assistant's [HomeKit Bridge](https://www.home-assistant.io/integrations/homekit/).
Google Home and Alexa work the same way through their own Home Assistant
integrations.

### When something changes on the wall unit

- **Device deleted:** it disappears from Home Assistant on its own.
- **Device added, renamed or moved to another room:** reload the integration
  (**Settings → Devices & services → eSMART →** ⋮ → **Reload**) or restart
  Home Assistant to pick up the change. Names you set yourself in Home
  Assistant are kept.
- **Empty room device left behind:** if a room no longer has any devices,
  open it in Home Assistant and delete it.

### When the connection drops

If Home Assistant loses the connection to the eSMART cloud, all eSMART
entities show as unavailable. The integration keeps trying to reconnect,
first after 5 seconds and then less often, up to every 5 minutes. You don't
need to do anything. Commands you send while it's offline are not saved for
later: Home Assistant shows an error and the command is dropped.

## Updating

When a new version is out, Home Assistant shows it at the top of the
**Settings** page, next to its own updates. Install it and restart Home
Assistant. Your pairing, entities and automations stay as they are. What
changed in each version is listed on the
[releases page](https://github.com/b2a3e8/ha-esmart/releases).

## Pairing again

You have to pair again if the remote gets deleted on the wall unit. Home
Assistant notices this and shows a prompt under **Settings → Devices &
services**. You can also pair again whenever you like: open the eSMART entry,
then ⋮ → **Reconfigure**.

Pairing again works like the first setup: start adding a remote on the wall
unit and type in the new code. Your entities, names and automations are kept.
If the old remote is still listed on the wall unit, delete it there first or
pick a different name, because the wall unit won't accept two remotes with
the same name.

## Removing the integration

1. Go to **Settings → Devices & services → eSMART**, open ⋮ and select
   **Delete**. Home Assistant also deletes its remote account on the eSMART
   server.
2. On the wall unit, delete the "Home Assistant" remote under **Settings →
   Remote access**. The wall unit doesn't always notice on its own.
3. If you want to remove the code as well, uninstall eSMART in the
   Marketplace and restart Home Assistant.

## Reporting a problem

1. **Download the diagnostics:** **Settings → Devices & services → eSMART →**
   ⋮ → **Download diagnostics**. This file leaves out your account details,
   your name, address and the apartment ID. Room and device names are kept,
   because they help match the report to your setup.
2. **If you need logs**, turn on debug logging on the same page (⋮ → **Enable
   debug logging**), make the problem happen again, then turn it off. Home
   Assistant then downloads the log for you.
3. **Clean up the log before you post it.** Unlike the diagnostics, the log
   is not cleaned up. It contains your installation ID (looks like
   `xx-xx-xx-…@conference.myesmart.net`), the remote's account ID
   (`i-…@myesmart.net`), your name, your postcode and your room and device
   names. Redact those.
4. Open a GitHub issue and attach both files.

If the problem happens during setup, there's no eSMART entry yet to turn on
debug logging from. Add this to `configuration.yaml` instead, restart Home
Assistant and try again. The log is under **Settings → System → Logs**.
Remove the lines again when you're done.

```yaml
logger:
  logs:
    custom_components.esmart: debug
```

## Known limitations

- **Tested on one apartment.** Everything here was worked out and tested on a
  single eSMART setup. Others may only work in part. If yours doesn't, please
  open an issue.
- **Limited device types:** cooling, ventilation and any device type not listed
  under [Features](#features). Please open an issue if your flat has unsupported
  device types and you're happy to help with some logs etc.
- **No support for video intercom**: the entrance doorbell and video intercom
  is not supported at the moment.
- **Pairing:** only pairing with the 4-digit code shown on the wall unit works.
  There are hints that the wall unit knows other pairing methods, but they
  aren't implemented.

## Contributing

Bug reports, device support requests and pull requests are welcome. How the
code is laid out, tested and released is in the
[developer documentation](docs/DEVELOPER.md). How the eSMART protocol works
is in [PROTOCOL.md](docs/PROTOCOL.md).

## License

[MIT](LICENSE)
