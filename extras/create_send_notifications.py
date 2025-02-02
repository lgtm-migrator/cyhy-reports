#!/usr/bin/env python

"""Create CyHy notifications and email them out to CyHy points of contact.

Usage:
  create_send_notifications [options] CYHY_DB_SECTION
  create_send_notifications (-h | --help)

Options:
  -h --help              Show this message.
  --log-level=LEVEL      If specified, then the log level will be set to
                         the specified value.  Valid values are "debug",
                         "info", "warning", "error", and "critical".
                         [default: warning]
"""

import distutils.dir_util
import logging
import os
import subprocess
import sys

import docopt

from cyhy.core import Config
from cyhy.db import database
from cyhy.util import util
from cyhy_report.cyhy_notification import NotificationGenerator

current_time = util.utcnow()

NOTIFICATIONS_BASE_DIR = "/var/cyhy/reports/output"
NOTIFICATION_ARCHIVE_DIR = os.path.join(
    "notification_archive", "notifications{}".format(current_time.strftime("%Y%m%d"))
)
CYHY_MAILER_DIR = "/var/cyhy/cyhy-mailer"


def create_output_directories():
    """Create all necessary output directories."""
    distutils.dir_util.mkpath(
        os.path.join(NOTIFICATIONS_BASE_DIR, NOTIFICATION_ARCHIVE_DIR)
    )


def build_cyhy_org_list(db):
    """Build list of CyHy organization IDs.

    This is the list of CyHy organization IDs (and their descendants) that
    receive CyHy reports.
    """
    cyhy_org_ids = set()  # Use a set here to avoid duplicates
    for cyhy_request in list(
        db.RequestDoc.collection.find(
            {"report_types": "CYHY"}, {"_id": 1, "children": 1}
        ).sort([("_id", 1)])
    ):
        cyhy_org_ids.add(cyhy_request["_id"])
        if cyhy_request.get("children"):
            cyhy_org_ids.update(db.RequestDoc.get_all_descendants(cyhy_request["_id"]))
    return list(cyhy_org_ids)


def generate_notification_pdfs(db, org_ids, master_report_key):
    """Generate all notification PDFs for a list of organizations."""
    num_pdfs_created = 0
    for org_id in org_ids:
        logging.info("{} - Starting to create notification PDF".format(org_id))
        generator = NotificationGenerator(
            db, org_id, final=True, encrypt_key=master_report_key
        )
        was_encrypted, results = generator.generate_notification()
        if was_encrypted:
            num_pdfs_created += 1
            logging.info("{} - Created encrypted notification PDF".format(org_id))
        elif results is not None and len(results["notifications"]) == 0:
            logging.info("{} - No notifications found, no PDF created".format(org_id))
        else:
            logging.error("{} - Unknown error occurred".format(org_id))
            return -1
    return num_pdfs_created


def main():
    """Set up logging and call the notification-related functions."""
    args = docopt.docopt(__doc__, version="1.0.0")
    # Set up logging
    log_level = args["--log-level"]
    try:
        logging.basicConfig(
            format="%(asctime)-15s %(levelname)s %(message)s", level=log_level.upper()
        )
    except ValueError:
        logging.critical(
            '"{}" is not a valid logging level.  Possible values '
            "are debug, info, warning, and error.".format(log_level)
        )
        return 1

    # Set up database connection
    db = database.db_from_config(args["CYHY_DB_SECTION"])

    # Create all necessary output subdirectories
    create_output_directories()

    # Change to the correct output directory
    os.chdir(os.path.join(NOTIFICATIONS_BASE_DIR, NOTIFICATION_ARCHIVE_DIR))

    # Build list of CyHy orgs
    cyhy_org_ids = build_cyhy_org_list(db)
    logging.debug("Found {} CYHY orgs: {}".format(len(cyhy_org_ids), cyhy_org_ids))

    # Create notification PDFs for CyHy orgs
    master_report_key = Config(args["CYHY_DB_SECTION"]).report_key
    num_pdfs_created = generate_notification_pdfs(db, cyhy_org_ids, master_report_key)
    logging.info("{} notification PDFs created".format(num_pdfs_created))

    # Create a symlink to the latest notifications.  This is for the
    # automated sending of notification emails.
    latest_notifications = os.path.join(
        NOTIFICATIONS_BASE_DIR, "notification_archive/latest"
    )
    if os.path.exists(latest_notifications):
        os.remove(latest_notifications)
    os.symlink(
        os.path.join(NOTIFICATIONS_BASE_DIR, NOTIFICATION_ARCHIVE_DIR),
        latest_notifications,
    )

    if num_pdfs_created:
        # Email all notification PDFs in
        # NOTIFICATIONS_BASE_DIR/notification_archive/latest
        os.chdir(CYHY_MAILER_DIR)
        p = subprocess.Popen(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.cyhy-notification.yml",
                "up",
            ],
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        data, err = p.communicate()
        return_code = p.returncode

        if return_code == 0:
            logging.info("Notification emails successfully sent")
        else:
            logging.error("Failed to email notifications")
            logging.error("Stderr report detail: %s%s", data, err)

        # Delete all NotificationDocs where generated_for is not []
        result = db.NotificationDoc.collection.delete_many(
            {"generated_for": {"$ne": []}}
        )
        logging.info(
            "Deleted {} notifications from DB (corresponding to "
            "those just emailed out)".format(result.deleted_count)
        )
    else:
        logging.info("Nothing to email - skipping this step")

    # Delete all NotificationDocs where ticket_owner is not a CyHy org, since
    # we are not currently sending out notifications for non-CyHy orgs
    result = db.NotificationDoc.collection.delete_many(
        {"ticket_owner": {"$nin": cyhy_org_ids}}
    )
    logging.info(
        "Deleted {} notifications from DB (owned by "
        "non-CyHy organizations, which do not currently receive "
        "notification emails)".format(result.deleted_count)
    )

    # Stop logging and clean up
    logging.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
