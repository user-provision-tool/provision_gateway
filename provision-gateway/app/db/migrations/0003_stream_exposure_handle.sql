-- T9 follow-up (2026-11-02): keep the exposure secret so an authorized user can
-- show the URL after minting. The handle label is encrypted at rest with the
-- gateway keyring (the envelope carries its key version); the sha256 lookup key
-- stays the primary key and is still what the stream door matches.
ALTER TABLE gateway.stream_exposures ADD COLUMN handle_enc bytea;

-- Rows minted before this column existed stored only the sha256, so their URL
-- can never be shown. Per the operator ruling, retire every live one at deploy;
-- the next Generate mints a fresh, showable row.
UPDATE gateway.stream_exposures SET revoked_at = now() WHERE revoked_at IS NULL;
