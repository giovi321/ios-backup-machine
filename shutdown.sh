#!/bin/bash
# Launch owner-message.service, then power off the system

systemctl start owner-message.service
shutdown -h now
