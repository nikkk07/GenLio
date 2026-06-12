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

create index if not exists idx_posts_date   on public.posts (date);
create index if not exists idx_posts_status on public.posts (status);

-- Row Level Security: dashboard (anon) gets read-only; writes come from the
-- service role key, which bypasses RLS.
alter table public.posts enable row level security;

drop policy if exists "posts_public_read" on public.posts;
create policy "posts_public_read"
    on public.posts for select
    to anon
    using (true);
