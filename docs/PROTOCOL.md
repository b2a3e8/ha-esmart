# eSMART protocol

The eSMART protocol isn't documented anywhere public. This is what was
worked out from the official apps and real traffic. How the integration is
built, tested and released is in [DEVELOPER.md](DEVELOPER.md).

## Overview

eSMART uses JSON messages sent over XMPP. The server is
`xmpp.myesmart.net`, port 5222, with STARTTLS.

Every installation has a Multi-User Chat room,
`<installation-id>@conference.myesmart.net`, which the wall unit and all
remotes join. The installation ID is the wall unit's MAC address. The wall
unit (sender `master`) pushes state as `INFO` messages and carries out `CMD`
messages. Remotes ask for the current state with `GET` messages.

Every message has the same envelope:

```json
{
  "headers": {
    "method": "CMD",
    "type": "operation",
    "version": "1.19.0",
    "to": "master",
    "from": "i-xxxxxxxxxxxxx-123@myesmart.net",
    "timestamp": "2026-01-01 12:00:00+0000",
    "size": "1"
  },
  "body": [{"id": 12, "onoff": "on"}]
}
```

`size` is the number of items (or keys) in `body`, as a string.

## Pairing and sessions

- **Account:** a remote registers its own account (XEP-0077). The wall unit
  expects user names shaped like `i-<13 characters>-<3 digits>`.
- **Pairing:** the remote joins the shared room
  `shared@conference.myesmart.net` and posts
  `{"jid": ..., "name": ..., "code": <number>}`, where `code` is the number
  the wall unit shows. The wall unit accepts by subscribing to the remote's
  presence, and the remote subscribes back.
- **Session start:** the remote sends presence with entity capabilities
  (XEP-0115), fetches its roster, joins each device
  room and sends `GET` for `capabilities`, `config`, `data`, `structure`,
  `doors` and `scene`.
- **Room joins** can fail with 407 (not a member yet, right after pairing)
  or 409 (nickname in use, for example by a stale session of the same
  account).

## Messages

| Type | Direction | Content |
|---|---|---|
| `structure` | `INFO` | Floors, rooms and nodes, with `id`, `name`, `type_id`, `type_name`, `has_orientation`, `reverted`. Also `tenant_name`. |
| `data` | `INFO` | Live telemetry: `nodes` (each with `id` and `data`) and `meters`. |
| `operation` | `CMD` / `INFO` | A list of node operations. The wall unit echoes each one back as `INFO/operation`. |
| `config` | `INFO` | Settings, including the heating setpoint range. |
| `doors` | `INFO` | The entrance doors, with `id` and `name`. |
| `door` | `CMD` | `{"id": <door>, "action": "pulse"}` opens a door. |
| `scene` | `INFO` / `CMD` | The scene list, and `{"num": ..., "version": ...}` to run a scene. |

## Details

- **Node types** (`type_id`): 2 lamp, 3 dimmable lamp, 4 blind, shutter or
  awning, 6 heating. Some firmwares localise `type_name` ("Lampe",
  "Store"), which is matched as a fallback.
- **Operations** carry the node `id` plus one or more of `onoff`
  (`"on"`/`"off"`), `dimming` (a string such as `"60%"`), `position`,
  `orientation`, `setpoint` and `stop: true`.
- **Blinds** use a 0 to 1024 tick scale: 0 is fully open, 1024 fully closed.
  Slat orientation uses the same scale. Awnings look exactly like blinds;
  only the name tells them apart.
- **Heating** telemetry carries `setpoint`, `temperature`, `deviceOnOff`
  (whether the valve is heating right now) and `power`, but no on/off state.
- **Setpoint limits** come from `INFO/config`: `setpoint_min_temp`,
  `setpoint_max_temp` and `setpoint_increment`.
- **Scenes** are run with their `num` and current `version`. Editing a scene
  on the wall unit changes its version. `active` means the scene is enabled,
  not that it is running. Only the summary (`GET scene` with
  `fullConfig: false`) lists every scene.
- **Meters:** one meter ID can report several readings as separate entries.
  Hot water, for example, has `power` and `flow_rate`. `power` is always live
  watts. The total meter is the sum of the others. The Wh and dL units in the
  structure describe counters that `data` doesn't carry.

## Doorbell and videophone (SIP, not implemented)

The doorbell **ring** and the two-way **videophone** call don't use the XMPP
messages above. They run over a separate SIP (VoIP) channel. That's why the
integration can open the door (`CMD/door`) but can't see a ring or take the
call.

What happens on the app side:

1. The wall unit or the eSMART server places a SIP call to the remote.
2. A VoIP push (PushKit/APNs on iOS) wakes the app.
3. The app registers with the SIP server using PJSIP, answers the `INVITE`
   and shows audio and video through CallKit.

A remote probably only gets calls once **videophone deviation** is turned on
for it on the wall unit.

However, the whole doorbell and videoconference situation needs some more
reverse-engineering to get more details for a reliable implementation. At the
moment it is out of scope.
