# Security

Do not commit production credentials, private keys, pairing state, or local
service tokens.

In particular, keep these out of the repository:

- `server/gadget_state.json` (Gadget device key / identity)
- `.env` and local systemd overrides containing API keys
- SSH private keys
- Hermes pairing/state databases
- captured request/response logs that may contain tokens

The distributable JEV drop-in is intentionally disabled and contains no
`TYPESAFE_API_KEY`.

If a credential is ever committed, rotate it first; removing it from the latest
Git commit is not sufficient because Git history still contains the old value.
