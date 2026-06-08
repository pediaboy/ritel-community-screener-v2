-- RITELCOMMUNITY.ID SCREENER — Schema Setup
-- Jalankan di Supabase SQL Editor

-- 1. Table users
CREATE TABLE IF NOT EXISTS public.users (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  phone_number text UNIQUE NOT NULL,
  name text,
  status text DEFAULT 'Free',
  created_at timestamptz DEFAULT now()
);

-- 2. Table stocks_data
CREATE TABLE IF NOT EXISTS public.stocks_data (
  ticker text PRIMARY KEY,
  price numeric,
  volume numeric,
  change_pct numeric,
  ma20 numeric,
  macd numeric,
  macd_signal numeric,
  updated_at timestamptz DEFAULT now()
);

-- 3. Table global_settings (1 baris saja)
CREATE TABLE IF NOT EXISTS public.global_settings (
  id int PRIMARY KEY DEFAULT 1,
  vip_price numeric DEFAULT 99000,
  bank_account text DEFAULT 'BCA 1234567890 a.n. Thirafi Thariq',
  wa_channel text DEFAULT '#',
  wa_group text DEFAULT '#',
  ig_link text DEFAULT '#'
);

-- Insert default row agar CMS tidak error
INSERT INTO public.global_settings (id, vip_price, bank_account, wa_channel, wa_group, ig_link)
VALUES (1, 99000, 'BCA 1234567890 a.n. Thirafi Thariq', '#', '#', '#')
ON CONFLICT (id) DO NOTHING;

-- Enable RLS tapi allow all untuk API (pakai service key)
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stocks_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.global_settings ENABLE ROW LEVEL SECURITY;

-- Allow anon read untuk stocks dan settings
CREATE POLICY "Public read stocks" ON public.stocks_data FOR SELECT USING (true);
CREATE POLICY "Public read settings" ON public.global_settings FOR SELECT USING (true);
-- Allow all untuk service role (backend)
CREATE POLICY "Service all users" ON public.users USING (true) WITH CHECK (true);
CREATE POLICY "Service all stocks" ON public.stocks_data USING (true) WITH CHECK (true);
CREATE POLICY "Service all settings" ON public.global_settings USING (true) WITH CHECK (true);
