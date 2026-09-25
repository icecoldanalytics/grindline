-- ── PROFILES ──────────────────────────────────────────────────────────
create table public.profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  display_name  text,
  email_opt_in  boolean not null default true,
  created_at    timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "users manage their own profile"
  on public.profiles for all
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- Auto-create a profile row the moment someone signs up, so the app never
-- has to remember to do it client-side after auth succeeds.
create function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.profiles (id) values (new.id);
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();


-- ── ROSTER_PLAYERS ────────────────────────────────────────────────────
create table public.roster_players (
  user_id       uuid not null references auth.users(id) on delete cascade,
  nhl_player_id integer not null,
  player_type   text not null check (player_type in ('skater', 'goalie')),
  added_at      timestamptz not null default now(),
  primary key (user_id, nhl_player_id)
);

alter table public.roster_players enable row level security;

create policy "users manage their own roster"
  on public.roster_players for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Enforces the 25-skater / 3-goalie caps in the database itself. Fires on
-- INSERT and UPDATE - the RLS policy above already lets a client UPDATE
-- an existing row (e.g. change player_type), so guarding INSERT alone
-- would leave that path free to blow past the cap.
create function public.enforce_roster_caps()
returns trigger
language plpgsql
as $$
declare
  cap integer := case new.player_type when 'skater' then 25 else 3 end;
  current_count integer;
begin
  if tg_op = 'INSERT' then
    select count(*) into current_count
      from public.roster_players
      where user_id = new.user_id and player_type = new.player_type;

  elsif tg_op = 'UPDATE' then
    if new.user_id = old.user_id and new.player_type = old.player_type then
      -- Nothing cap-relevant changed (e.g. just added_at, or swapping
      -- which player fills this slot without changing its type) - skip.
      return new;
    end if;
    select count(*) into current_count
      from public.roster_players
      where user_id = new.user_id
        and player_type = new.player_type
        and not (user_id = old.user_id and nhl_player_id = old.nhl_player_id);
  end if;

  if current_count >= cap then
    raise exception 'roster cap reached for %: % of % already on roster', new.player_type, current_count, cap;
  end if;
  return new;
end;
$$;

create trigger roster_cap_check
  before insert or update on public.roster_players
  for each row execute function public.enforce_roster_caps();
