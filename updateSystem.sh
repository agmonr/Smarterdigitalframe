#!/bin/bash
mount -o remount,rw /boot/firmware
apt update
apt -y dist-upgrade
mount -o remount,ro /boot/firmware
