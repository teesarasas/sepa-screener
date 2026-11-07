-- SEPA trade journal for Supabase.
-- Run once: Supabase dashboard -> SQL Editor -> New query -> paste -> Run.
-- Adds two new tables; your existing tables are not touched.

-- One row per position (a buy, possibly scaled out of in several sells)
create table if not exists public.stock_trades (
  id              bigint generated always as identity primary key,
  created_at      timestamptz not null default now(),
  symbol          text        not null,
  entry_date      date        not null,
  entry_price     numeric(12,4) not null check (entry_price > 0),
  shares          numeric(14,4) not null check (shares > 0),
  initial_stop    numeric(12,4) not null check (initial_stop > 0),
  current_stop    numeric(12,4),            -- raised over time (breakeven, back stop)
  status          text        not null default 'open' check (status in ('open', 'closed')),

  -- what the setup looked like at entry, so results can be split by setup quality
  signal_at_entry text,                     -- Breakout / Ready / Extended / other
  pivot           numeric(12,4),
  rs_rating       smallint,
  rs_line         text,                     -- 'leads' / 'high' / 'no'
  eps_pass        boolean,
  market_state    text,                     -- 'uptrend' / 'correction' / 'downtrend'
  sector          text,
  notes           text
);

-- One row per sell (partial or full)
create table if not exists public.stock_trade_exits (
  id          bigint generated always as identity primary key,
  trade_id    bigint not null references public.stock_trades(id) on delete cascade,
  exit_date   date   not null,
  exit_price  numeric(12,4) not null check (exit_price > 0),
  shares      numeric(14,4) not null check (shares > 0),
  reason      text,                         -- e.g. '2R half', 'back stop', 'stop', 'earnings'
  created_at  timestamptz not null default now()
);

create index if not exists stock_trades_status_idx on public.stock_trades (status);
create index if not exists stock_trade_exits_trade_idx on public.stock_trade_exits (trade_id);

-- Row Level Security ON with no policies: the public 'anon' key can't read or write
-- these tables at all. The dashboard and the GitHub job use the service_role key,
-- kept only in Streamlit / GitHub secrets.
alter table public.stock_trades enable row level security;
alter table public.stock_trade_exits enable row level security;
