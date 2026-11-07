# Developer documentation

How the integration is built, tested, debugged and released. The eSMART
protocol itself is described in [PROTOCOL.md](PROTOCOL.md).

## Code layout

Everything lives in [`custom_components/esmart`](../custom_components/esmart).

| File | What it does |
|---|---|
| [`client.py`](../custom_components/esmart/client.py) | The XMPP connection, built on `slixmpp`: account registration, pairing, room joins, reconnects, the health ping and sending commands. No Home Assistant imports. |
| [`protocol.py`](../custom_components/esmart/protocol.py) | The wire format: the node model, device classification, parsing and message builders. No Home Assistant or XMPP imports. |
| [`coordinator.py`](../custom_components/esmart/coordinator.py) | `ESmartHub`: turns incoming messages into entity state, adds and removes devices, and turns client errors into Home Assistant errors. |
| [`entity.py`](../custom_components/esmart/entity.py) | Entity base classes and the setup code all platforms share. |
| `light.py`, `cover.py`, `climate.py`, `button.py`, `scene.py`, `sensor.py` | One file per entity platform. |
| [`config_flow.py`](../custom_components/esmart/config_flow.py) | Pairing, and pairing again (reauth and reconfigure). |
| [`diagnostics.py`](../custom_components/esmart/diagnostics.py) | The redacted diagnostics download. |
| `strings.json`, `translations/en.json` | UI texts. Custom integrations load `translations/en.json`, so keep both identical (a test checks this). |

## Connection lifecycle

The handshake itself is in [PROTOCOL.md](PROTOCOL.md#pairing-and-sessions).
On top of it, the integration does this:

- **Pairing:** if pairing fails after the account was registered, the account
  is deleted again. Pairing again (reauth or reconfigure) registers a new
  account and deletes the old one once the entry has reloaded.
- **Room joins:** a refused join is retried after 15 s, 30 s, 1 min, 2 min
  and 5 min. A 409 (nickname taken, usually by our own stale session)
  switches to a unique nickname. If the wall unit still answers 407 after the
  last retry, a repair issue asks the user to pair again. The retries start
  over on every reconnect.
- **Reconnects:** after an unexpected disconnect the client retries with
  exponential backoff, from 5 s up to 5 min. slixmpp's own retries are
  cancelled whenever a connect attempt fails, so only one retry loop ever
  runs. A ping every 2 minutes catches links that died silently.
- **Rejected credentials** (SASL `not-authorized` and similar) stop the
  retries and start Home Assistant's reauth flow.
- **Commands** raise `NotConnected` while the session is down. The hub turns
  that into a `HomeAssistantError` the user sees. Node operations sent within
  20 ms of each other are merged into one `CMD/operation` message, so a scene
  with many lights switches them all at once. A stop is sent immediately.

## Development setup

The tests run on Python 3.14, like CI.

```bash
python3.14 -m venv .venv
. .venv/bin/activate
pip install -r requirements_test.txt pytest-cov ruff "slixmpp>=1.17.0"
pytest --cov=custom_components/esmart
ruff format . && ruff check .
```

`requirements_test.txt` pins the test harness to a known Home Assistant
release. `slixmpp` comes from the manifest's `requirements`, so keep the
version above in sync with it.

The tests never touch the network: `tests/conftest.py` replaces the XMPP
client with a fake that replays the sanitized messages in `tests/fixtures/`.

CI runs the tests against the pinned harness. It's meant to also run them
against the oldest Home Assistant that `hacs.json` allows, but there's no
harness for 2026.11.0 yet, so that job is missing for now (see the comment in
[`tests.yml`](../.github/workflows/tests.yml)). Dependabot bumps
`requirements_test.txt` once a newer harness is out.

To try a change in a real Home Assistant, copy or symlink
`custom_components/esmart` into its `config/custom_components` folder and
restart Home Assistant.

## Debugging

Turn on debug logging for `custom_components.esmart`, either on the
integration page (⋮ → **Enable debug logging**) or in `configuration.yaml`
if you need it during setup:

```yaml
logger:
  logs:
    custom_components.esmart: debug
```

The log then shows:

- connection, pairing and room-join steps, with the reason for any failure;
- `RECV …` lines: every message in the device room and who sent it (`master`
  is the wall unit; other remotes, such as the app, show up with their
  account ID);
- `SEND …` lines: every command Home Assistant sends;
- `XMPP RECV …` / `XMPP SEND …` lines: the raw XML of every stanza. The SASL
  login exchange is left out. Don't turn on `slixmpp: debug` for logs you
  want to share, because that logs the login unredacted;
- every discovered node, and every node skipped because its type isn't
  supported.

The raw stanzas contain the tenant name, postcode, installation ID and
account IDs. Clean them up before posting a log anywhere.

### Blinds

Blinds log two extra kinds of line:

- **`COVER cmd`**: what Home Assistant was asked to do, the target in ticks
  and the last reported position and orientation. The `SEND CMD/operation`
  line right after it shows exactly what went out.
- **`COVER state`**: a blind's raw telemetry whenever it changes. `via
  operation` is the wall unit echoing a command, `via data` is telemetry.
  Neither tracks movement: `position` jumps to the target straight away, and
  `power` keeps its last reading after the motor stops.

To compare Home Assistant with the wall unit or the app, open and close the
same blind fully from each, waiting for the motor to stop every time, then:

```bash
grep -E 'COVER|Discovered cover|/operation' home-assistant.log
```

## Capturing traffic safely

New device types are added from real messages. When you capture traffic:

- **Never commit captures.** `.gitignore` blocks `*.pcap`, `*.pcapng`,
  `xmpp-examples/` and `captures/`, but check before every commit anyway.
- Captures contain the SASL login exchange (enough to attack the password
  offline), your installation ID (the wall unit's MAC address), account IDs,
  your name (`tenant_name`), postcode and room names.
- For tests, write a small fixture by hand that keeps the *shape* of the
  message and replaces every identifier and name, like the files in
  `tests/fixtures/`.

## Releasing

1. Update `version` in `custom_components/esmart/manifest.json`.
2. Commit, tag (`v1.2.3`) and publish a GitHub release from the tag, with a
   short note on what changed. A tag alone isn't enough: the Marketplace
   offers the latest *published* release, and the manifest `version` has to
   match it (the Marketplace won't install a release without one).
3. If the release needs a newer Home Assistant, raise `homeassistant` in
   `hacs.json`, the "What you need" section of the README, and the oldest
   pinned harness in `tests.yml`. The Marketplace keeps users on older Home
   Assistant versions on the last release that supports them.

The Marketplace (Home Assistant 2026.11+) still reads `hacs.json` and is
validated by `hacs/action`, so both stay. See the
[Marketplace publishing docs](https://contributors.home-assistant.io/marketplace/).

The `Marketplace` job in `validate.yml` runs every check, none ignored: the
catalog doesn't accept skipped checks. The `brands` check needs
`custom_components/esmart/brand/icon.png`.
