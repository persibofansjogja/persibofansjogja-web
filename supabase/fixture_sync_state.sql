-- Hybrid sync monitoring state. This table stores metadata only; it never stores or deletes fixtures.
create table if not exists public.fixture_sync_state (
    id smallint primary key check (id = 1),
    last_checked_at timestamptz,
    last_status text not null default 'never',
    last_source text,
    last_message text,
    next_run_at timestamptz,
    interval_minutes integer,
    found_count integer not null default 0,
    saved_count integer not null default 0,
    skipped_manual integer not null default 0,
    updated_at timestamptz not null default now()
);

alter table public.fixture_sync_state enable row level security;

revoke all on table public.fixture_sync_state from anon, authenticated;
grant usage on schema public to service_role;
grant select, insert, update, delete on table public.fixture_sync_state to service_role;

insert into public.fixture_sync_state (id, last_status, last_message)
values (1, 'never', 'Belum ada sinkronisasi otomatis.')
on conflict (id) do nothing;
