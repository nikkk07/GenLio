-- gelio dashboard schema (Phase 3)
-- Run this in the Supabase SQL editor, then create a PUBLIC storage bucket
-- named `slides` (Storage -> New bucket -> Public).
--
-- The gelio process writes with the SERVICE ROLE key (bypasses RLS). The
-- dashboard reads with the anon key, so we enable RLS and add a read-only
-- policy for anon. Never expose the service role key to the browser.

create table if not exists public.posts (
    id                 text primary key,
    date               text        not null,
    concept            text        not null,
    aviation_angle     text,
    status             text        not null,
    captions           jsonb,
    hashtags           jsonb,
    slide_urls         jsonb,
    pdf_url            text,
    regeneration_count integer     not null default 0,
    parent_id          text,
    created_at         text,
    updated_at         text
);

-- Phase 4 additions (safe to re-run on an existing project).
alter table public.posts add column if not exists scheduled_time text;
alter table public.posts add column if not exists x_post_id      text;
alter table public.posts add column if not exists ig_media_id    text;
alter table public.posts add column if not exists handled_by     text;

-- Phase 5: Supabase is now the AUTHORITATIVE state store (GELIO_STATE=supabase),
-- not just a dashboard mirror, so the table must hold every PostRecord column.
-- The SQLite `state` column maps to this table's `status` column; all others
-- keep their names. Safe to re-run.
alter table public.posts add column if not exists brief_path      text;
alter table public.posts add column if not exists content_path    text;
alter table public.posts add column if not exists rendered_at     text;
alter table public.posts add column if not exists x_status        text;
alter table public.posts add column if not exists ig_status       text;
alter table public.posts add column if not exists linkedin_status text;

create index if not exists idx_posts_date    on public.posts (date);
create index if not exists idx_posts_status  on public.posts (status);
create index if not exists idx_posts_concept on public.posts (concept);

-- Phase 5: concept dedup table. A concept is "used" the moment gelio drafts a
-- post for it; the topic engine excludes used concepts so it never repeats one.
create table if not exists public.used_concepts (
    concept    text primary key,
    created_at timestamptz not null default now()
);

-- Row Level Security: dashboard (anon) gets read-only; writes come from the
-- service role key, which bypasses RLS.
alter table public.posts enable row level security;
alter table public.used_concepts enable row level security;

drop policy if exists "posts_public_read" on public.posts;
create policy "posts_public_read"
    on public.posts for select
    to anon
    using (true);
