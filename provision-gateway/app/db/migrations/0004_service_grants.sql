-- G83 — directional admin-assigned service grants (2026-11-03).
--
-- One row says "the source service may call the target service". The account
-- grant (grantee account -> shared account) is the prerequisite for a
-- cross-owner pair; same-owner and public targets are implicit and therefore
-- never need a row. There is no transitivity and no symmetric pair.
CREATE TABLE gateway.service_grants (
    id                bigserial PRIMARY KEY,
    source_service_id uuid NOT NULL REFERENCES registry.services(id),
    target_service_id uuid NOT NULL REFERENCES registry.services(id),
    created_by        uuid REFERENCES gateway.users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT service_grants_not_self CHECK (source_service_id <> target_service_id),
    CONSTRAINT service_grants_pair UNIQUE (source_service_id, target_service_id)
);

CREATE INDEX service_grants_by_source ON gateway.service_grants (source_service_id);
CREATE INDEX service_grants_by_target ON gateway.service_grants (target_service_id);
