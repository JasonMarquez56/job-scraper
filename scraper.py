#!/usr/bin/env python3

import sqlite3
import logging
import sys
import time
import re
from datetime import datetime, timezone
from pathlib import Path

import os

import yaml
import requests
import pandas as pd
from dotenv import load_dotenv
from jobspy import scrape_jobs

load_dotenv()

# Logging

def setup_logging(cfg: dict):
	level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
	handlers = [logging.StreamHandler(sys.stdout)]
	log_file = cfg.get("log_file")
	if log_file:
		handlers.append(logging.FileHandler(log_file))
	logging.basicConfig(
		level=level,
		format="%(asctime)s [%(levelname)s] %(message)s",
		handlers=handlers,
	)

log = logging.getLogger(__name__)

# Config

def load_config(path: str = "config.yaml") -> dict:
	with open(path) as f:
		return yaml.safe_load(f)

# Database

def init_db(db_path: str) -> sqlite3.Connection:
	conn = sqlite3.connect(db_path)
	conn.execute("""
		CREATE TABLE IF NOT EXISTS seen_jobs (
			job_url TEXT PRIMARY KEY,
			title TEXT,
			company TEXT,
			seen_at TEXT
		)
	""")
	conn.commit()
	return conn

def is_new_job(conn: sqlite3.Connection, job_url: str) -> bool:
	row = conn.execute("SELECT 1 FROM seen_jobs WHERE job_url = ?", (job_url,)).fetchone()
	return row is None

def mark_seen(conn: sqlite3.Connection, job_url: str, title: str, company: str):
	conn.execute(
		"INSERT OR IGNORE INTO seen_jobs (job_url, title, company, seen_at) VALUES (?, ?, ?, ?)",
		(job_url, title, company, datetime.now(timezone.utc).isoformat()),
	)
	conn.commit()

# Scraping

def scrape(search: dict, results_wanted: int = 50) -> pd.DataFrame:
	log.info(f"Scraping: '{search['search_term']}' @ '{search['location']}'")
	try:
		jobs = scrape_jobs(
			site_name=["linkedin"],
			search_term=search["search_term"],
			location=search["location"],
			is_remote=search.get("is_remote", False),
			results_wanted=results_wanted,
			hours_old=14 * 24,
			linkedin_fetch_description=True,
		)
		log.info(f" -> {len(jobs)} raw results")
		return jobs
	except Exception as e:
		log.error(f" Scrape failed: {e}")
		return pd.DataFrame()
	
# Filtering

def passes_filters(row: pd.Series, cfg: dict) -> tuple[bool, str]:
	title = str(row.get("title", "")).lower()
	company = str(row.get("company", "")).lower()
	description = str(row.get("description", "")).lower()

	# Exclude companies
	for exec_co in cfg.get("excluded_companies", []):
		if exec_co.lower() in company:
			return False, f"excluded-company:{exec_co}"
		
	# Exclude keywords
	for kw in cfg.get("excluded_keywords", []):
		if kw.lower() in title or kw.lower() in description:
			return False, f"excluded-keyword:{kw}"
		
	# Must contain at least one required keyword
	required = cfg.get("required_keywords", [])
	if required:
		text = title + " " + description
		if not any(kw.lower() in text for kw in required):
			return False, "no-required-keywords"
	
	return True, "ok"

# Discord

COLORS = {
	"fulltime": 0x5865F2, 	# Discord blurple
	"parttime": 0xFEE75C, 	# Yellow
	"contract": 0xED4245, 	# Red
	"internship": 0x57F287, # Green
	"default": 0x23272A,		# Dark
}

def build_embed(row: pd.Series) -> dict:
	title = row.get("title", "Unknown Title")
	company = row.get("company", "Unknown Company")
	location = row.get("location", "")
	job_url = row.get("job_url", "")
	job_type = str(row.get("job_type", "")).lower()
	date_posted = row.get("date_posted", "")
	min_amount = row.get("min_amount")
	max_amount = row.get("max_amount")
	interval = row.get("interval", "")

	# Salary line
	salary = ""
	if pd.notna(min_amount) and pd.notna(max_amount):
		salary = f"${int(min_amount):,} - ${int(max_amount):,}"
		if interval:
			salary += f" /{interval}"
	elif pd.notna(min_amount):
		salary = f"${int(min_amount):,}+ /{interval}" if interval else f"${int(min_amount):,}+"
	
	# Description
	desc = str(row.get("description", "")).replace("\n", " ")
	snippet = desc[:300].strip()
	if len(desc) > 300:
		snippet += "..."
	
	fields = []
	if location:
		fields.append({"name": "📍 Location", "value": location, "inline": True})
	if job_type:
		fields.append({"name": "🗂 Type", "value": job_type.title(), "inline": True})
	if date_posted:
		fields.append({"name": "📅 Posted", "value": str(date_posted), "inline": True})
	if salary:
		fields.append({"name": "💰 Salary", "value": salary, "inline": True})
	if snippet:
		fields.append({"name": "📝 Snippet", "value": snippet, "inline": False})
	
	color = COLORS.get(job_type, COLORS["default"])

	embed = {
		"title": f"{title} @ {company}",
		"url": job_url,
		"color": color,
		"fields": fields,
		"footer": {"text": "LinkedIn Job Alert • " + datetime.now().strftime("%Y-%m-%d")},
	}
	return embed

def send_to_discord(webhook_url: str, embeds: list[dict]):
	if not embeds:
		return
	payload = {"embeds": embeds[:10]}
	r = requests.post(webhook_url, json=payload, timeout=15)
	if r.status_code not in (200, 204):
		log.error(f"Discord error {r.status_code}: {r.text[:200]}")
	else:
		log.info(f" Sent {len(embeds)} embeds to Discord")
	time.sleep(1)

def send_summary_message(webhook_url: str, total: int):
	payload = {
		"content": f"**Daily Job Report** - Found **{total}** new job(s) matching your filters."
	}
	requests.post(webhook_url, json=payload, timeout=15)

def send_no_results_message(webhook_url: str):
	payload = {"content": "No new matching jobs found today. Check back tomorrow!"}
	requests.post(webhook_url, json=payload, timeout=15)

# Main

def run(config_path: str = "config.yaml"):
	cfg = load_config(config_path)
	setup_logging(cfg)
	log.info("=" * 60)
	log.info("LinkedIn Job Scraper starting")

	conn = init_db(cfg.get("db_path", "seen_jobs.db"))
	webhook_url = os.getenv("DISCORD_WEBHOOK_URL") or cfg["discord"]["webhook_url"]
	jobs_per_msg = cfg["discord"].get("jobs_per_message", 10)
	results_wanted = cfg.get("results_per_search", 50)

	all_new_jobs = []

	for search in cfg.get("searches", []):
		df = scrape(search, results_wanted=results_wanted)
		if df.empty:
			continue

		for _, row in df.iterrows():
			job_url = str(row.get("job_url", ""))
			if not job_url:
				continue
		
			# Duplicate check
			if not is_new_job(conn, job_url):
				continue

			# Filter check
			ok, reason = passes_filters(row, cfg)
			if not ok:
				log.debug(f" SKIP [{reason}] {row.get('title')} @ {row.get('company')}")
				continue

			log.info(f" NEW JOB: {row.get('title')} @ {row.get('company')}")
			all_new_jobs.append(row)
			mark_seen(conn, job_url, str(row.get("title", "")), str(row.get("company", "")))
		
		time.sleep(3)

	log.info(f"Total new jobs to send: {len(all_new_jobs)}")

	# Sort all new jobs by newest
	all_new_jobs.sort(
		key=lambda row: pd.to_datetime(row.get("date_posted"), errors="coerce") or pd.Timestamp.min,
		reverse=True
	)

	if not all_new_jobs:
		send_no_results_message(webhook_url)
	else:
		send_summary_message(webhook_url, len(all_new_jobs))
		# Send in batches of jobs_per_message
		for i in range(0, len(all_new_jobs), jobs_per_msg):
			batch = all_new_jobs[i : i + jobs_per_msg]
			embeds = [build_embed(row) for row in batch]
			send_to_discord(webhook_url, embeds)
			time.sleep(2)
	
	conn.close()
	log.info("Done.")

if __name__ == "__main__":
	import argparses
	parser = argparse.ArgumentParser(description="LinkedIn Job Scraper")
	parser.add_argument("--config", default="config.yaml", help="Path to config file")
	parser.add_arguemnt("--reset", action="store_true", help="Clear the seen jobs database and exit")
	args = parser.parse_args()

	if args.reset:
		cfg = load_config(args.config)
		db_path = cfg.get("db_path", "seen_jobs.db")
		Path(db_path).unlink(missing_ok=True)
		print(f"Cleared seen jobs database: {db_path}")
	else:
		run(args.config)