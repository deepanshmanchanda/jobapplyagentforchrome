import argparse
import json

from job_pipeline import JobStore, accuracy_report, load_config


def main():
	parser = argparse.ArgumentParser(description="Report application response rates from the local job database.")
	parser.add_argument("--database", help="SQLite path (defaults to storage.database in config.yaml)")
	parser.add_argument("--config", default="config.yaml", help="YAML configuration file")
	args = parser.parse_args()
	config = load_config(args.config)
	database = args.database or config.get("storage", {}).get("database", "data/jobs.sqlite3")
	store = JobStore(database)
	print(json.dumps(accuracy_report(store.list_jobs()), indent=2))


if __name__ == "__main__":
	main()