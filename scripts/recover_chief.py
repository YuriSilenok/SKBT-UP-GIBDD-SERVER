"""Local administrator recovery. Never expose this command as a public API.

After loss of the last CHIEF's device, register a new employee installation,
verify its displayed QR/device UUID, then run this command on the database host.
The database administrator is the authorization boundary, not a client fingerprint.
"""
import argparse
from uuid import UUID
from app.db.database import init_database, close_database
from app.models.device import Device
from app.schemas.device import DeviceRole
from app.services.roles import change_role


def main():
    parser = argparse.ArgumentParser(description="Promote a verified employee device during administrator-assisted recovery")
    parser.add_argument("device_id", type=UUID)
    args = parser.parse_args()
    init_database()
    try:
        device = Device.get_by_id(args.device_id)
        if device.client_app != "employee":
            raise SystemExit("Only a registered employee installation can be recovered")
        change_role(actor=None, target=device, new_role=DeviceRole.CHIEF.value)
        print("Verified employee device now has CHIEF. No access token was disclosed or reset.")
    finally:
        close_database()


if __name__ == "__main__":
    main()
