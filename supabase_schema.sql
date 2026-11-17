-- Run this SQL once in your Supabase Project -> SQL Editor

create table if not exists public.app_kv (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.booking_history (
  id bigserial primary key,
  booked_at text not null,
  user_name text not null,
  station text not null,
  slot text not null,
  fuel text not null,
  tracking text not null,
  created_at timestamptz not null default now()
);
