from flask import current_app, abort
from datetime import datetime
import time
import json
from pymavlink import mavutil
import math
import threading

from src.library.util import get_distance_metres, get_point_further_away, get_degrees_needed_to_turn, empty_socket
import src.library.telemetry
from src.library.location import Location
from src.library.waypoints import Waypoints

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

GCOM_TELEMETRY_ENDPOINT = "http://host.docker.internal:8080/api/interop/telemetry"

class Vehicle:
    def __init__(self):
        self.reroute_thread = None
        self.mavlink_connection = None
        self.telemetry = None
        self.waypoint_loader = None
        self.connecting = False

        # For tracking when to pause logs when input is required for battery_rtl
        self.pause_logs = False

        # For exiting threads that don't need to keep running in the case of RTL from the battery or lack of rc connection
        self.returning_home = False

    # Threaded: Continuously post telemetry data to GCOM-X
    def post_to_gcom(self):
        while True:
            try:
                location = vehicle.get_location()

                http = requests.Session()
                retry = Retry(total=None, backoff_factor=1)
                adapter = HTTPAdapter(max_retries=retry)
                http.mount('http://', adapter)

                gcom_telemetry_post = http.post(
                    GCOM_TELEMETRY_ENDPOINT,
                    headers={ 'content-type': 'application/json', 'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X x.y; rv:42.0) Gecko/20100101 Firefox/42.0' },
                    data=json.dumps({
                        "latitude_dege7":  location["lat"]*10**7,
                        "longitude_dege7": location["lng"]*10**7,
                        "altitude_msl_m":  location["alt"],
                        "heading_deg":     vehicle.get_heading(),
                        "groundspeed_m_s": vehicle.get_speed(),
                        "chan3_raw":       vehicle.get_rc_channel()
                    }),
                    timeout=3
                )

                if gcom_telemetry_post.status_code == 200:
                    if self.pause_logs == False:
                        print("[OK]       GCOM-X Telemetry  POST")
                else:
                    if self.pause_logs == False:
                        print("[FAIL]     GCOM-X Telemetry  POST: " + str(gcom_telemetry_post.status_code))

            except Exception as e:
                if self.pause_logs == False:
                    print("[ERROR]    GCOM-X Telemetry  Exception encountered: " + str(e))

            time.sleep(0.1)

    # Threaded: For tracking RC connection and RTL when disconnected for 30s
    def rc_disconnect_monitor(self):
        disconnect_timer = False
        rc_threshold = 2000

        while True:
            # See details in variable declaration above
            if self.returning_home:
                return

            channel = vehicle.get_rc_channel()
            if channel < rc_threshold and disconnect_timer == False:
                disconnect_timer = True
                orig_time = datetime.now()
                print("[ALERT]    RC Connection     Lost!")
            elif channel < rc_threshold and disconnect_timer:
                curr_time = datetime.now()
                if self.pause_logs == False:
                    print("[ALERT]    RC Connection     Disconnected:", round((curr_time - orig_time).total_seconds(),1), "s")
                if (curr_time - orig_time).total_seconds() > 30:
                    vehicle.set_rtl()
                    self.returning_home = True
                    print("[EXPIRED]  RC Connection     Aircraft returning home to land!")
                    return
            else:
                disconnect_timer = False
                if self.pause_logs == False:
                    print("[OK]       RC Connection")
            time.sleep(0.5)

    # Threaded: For tracking flight time and RTL after 20 min (with the option to extend)
    def battery_rtl(self):
        takeoff_time = datetime.now()
        time_threshold = 1200 # 20 minutes in seconds
        add_time = "A"

        while True:
            # See details in variable declaration above
            if self.returning_home:
                return

            curr_time = datetime.now()
            time_delta = (curr_time - takeoff_time).total_seconds()
            if self.pause_logs == False:
                print("[OK]       Battery           Time since takeoff: ", int(time_delta // 60), "min", round(time_delta % 60), "s")

            if (curr_time - takeoff_time).total_seconds() > time_threshold:
                print("[CRITICAL] Battery           20 minute timer reached!")
                self.pause_logs = True
                print("------------------------------------------------------")
                choice = input("[CRITICAL] Battery          Do you want to extend the flight (y/n)? ")
                if choice.lower() == "y" or choice.lower() == "yes":
                    while not add_time.isnumeric():
                        add_time = input("[CRITICAL] Battery            For how many minutes to you want to extend for? ")
                        try:
                            time_threshold += (float(add_time) * 60)
                            self.pause_logs = False
                            print("------------------------------------------------------")
                        except:
                            print("[ERROR]    Battery           Invalid entry")
                else:
                    vehicle.set_rtl()
                    self.returning_home = True
                    print("------------------------------------------------------")
                    print("[CRITICAL] Battery           Returning to land")
                    self.pause_logs = False

                    # Kill rc and rover threads since no longer needed
                    
                    return
            time.sleep(1)

    # Threaded: Gets the target rover drop-off and initiates drop automatically when the drone reaches that position
    def rover_automation(self):
        # Call function to connect to Arduino here (like arduinoconnector.py in Stalker)
        # Connect to the winch by sending “uas” and reading “uas” returned

        target = Location(0, 0, 0)

        while target.lat == 0 and target.lng == 0 and target.alt == 0:
            target = Location(self.waypoints.airdrop["lat"], self.waypoints.airdrop["lng"], self.waypoints.airdrop["alt"])
            if self.pause_logs == False:
                print("[ALERT]    Rover & Winch     Waiting for target position")
            time.sleep(1)
        print("[ALERT]    Rover & Winch     Target position found!")

        # Will need to get target location from Interop mission
        # target = Location(38.14471510, -76.42795610, 0) # Lat and Long of center of target zone
        allowed_radius = 1.5 # Radius acceptable from target location

        while True:
            # See details in variable declaration above
            if self.returning_home:
                return

            try:
                location = vehicle.get_location()
            except:
                print("[ERROR]    Rover & Winch     Failed to get location")
            try:
                curr_loc = Location(location["lat"], location["lng"], location["alt"])
                dist = get_distance_metres(target, curr_loc)
                if self.pause_logs == False:
                    print("[OK]       Rover & Winch     distance from target: ", round(dist, 2), "m")
                if dist < allowed_radius:
                    vehicle.set_loiter()
                    print("[ALERT]    Rover & Winch     In target distance; Loitering")

                    # Send “AIRDROPBEGIN” to the winch
                    print("[START]    Rover & Winch     Starting deployment")

                    # Wait for winch to return “AIRDROPCOMPLETE”
                    print("[FINISH]   Rover & Winch     Task completed")

                    # Return to the mission in auto mode
                    vehicle.set_auto()
                    return
            except:
                print("[ERROR]    Rover & Winch     Function failure")
            time.sleep(0.1)

    def setup_mavlink_connection(self, connection, address, port=None, baud=57600):
        if self.mavlink_connection == None or self.mavlink_connection.target_system < 1 and not self.connecting:
            self.connecting = True
            current_app.logger.info("Mavlink connection is now being initialized")
            if connection == "tcp":
                self.mavlink_connection = mavutil.mavlink_connection(connection + ':' + address + ':' + str(port))
            elif connection == "serial":
                self.mavlink_connection = mavutil.mavlink_connection(address, baud=baud)
            else:
                raise Exception("Invalid connection type")
            self.mavlink_connection.wait_heartbeat(timeout=5)
            current_app.logger.info("Heartbeat from system (system %u component %u)" % (self.mavlink_connection.target_system, self.mavlink_connection.target_component))
            # init telemetry
            self.telemetry = src.library.telemetry.Telemetry(self)

            # init waypoints
            self.waypoints = Waypoints(self)

            # connection established, vehicle initialized
            # begin eternally posting telemetry to GCOM
            # via an eternal thread
            with current_app.app_context():
                post_to_gcom_thread = threading.Thread(target = self.post_to_gcom, daemon=True)
                post_to_gcom_thread.start()
                rc_disconnect_monitor_thread = threading.Thread(target = self.rc_disconnect_monitor, daemon=True)
                rc_disconnect_monitor_thread.start()
                battery_rtl_thread = threading.Thread(target = self.battery_rtl, daemon=True)
                battery_rtl_thread.start()
                rover_automation_thread = threading.Thread(target = self.rover_automation, daemon=True)
                rover_automation_thread.start()

        if self.mavlink_connection.target_system < 1:
            raise Exception("Mavlink is not connected")

    def disconnect(self):
        if self.mavlink_connection is not None:
            self.mavlink_connection.close()

    def is_connected(self):
        return self.mavlink_connection is not None

    def arm(self):
        self.mavlink_connection.arducopter_arm()

    def disarm(self):
        self.mavlink_connection.arducopter_disarm()

    def set_guided(self):
        self.mavlink_connection.set_mode('GUIDED')

    def set_auto(self):
        self.mavlink_connection.set_mode('AUTO')

    def set_rtl(self):
        vehicle.mavlink_connection.set_mode('RTL')

    def set_loiter(self):
        vehicle.mavlink_connection.set_mode('LOITER')

    def reroute(self, points):
        self.reroute_thread = threading.Thread(target = self.start_reroute, args=[points], daemon=True)
        self.reroute_thread.start()

    def stop_reroute(self):
        self.reroute_thread

    def get_location(self):
        self.telemetry.wait('GPS_RAW_INT')
        self.telemetry.wait('GLOBAL_POSITION_INT')
        return Location(self.telemetry.lat,
                        self.telemetry.lng,
                        self.telemetry.alt).__dict__

    def get_speed(self):
        self.telemetry.wait('VFR_HUD')
        return self.telemetry.groundspeed

    def get_heading(self):
        self.telemetry.wait('GLOBAL_POSITION_INT')
        return self.telemetry.heading

    def get_rc_channel(self):
        self.telemetry.wait('RC_CHANNELS_RAW')
        return self.telemetry.chan3_raw

    def fly_to(self, lat, lng, alt):
        frame = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT
        self.mavlink_connection.mav.mission_item_send(0, 0, 0, frame,
            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
            2, # current wp - guided command
            0,
            0,
            0,
            0,
            0,
            lat,
            lng,
            alt
        )

    def start_reroute(self, points):
        self.set_guided()
        for index, point in enumerate(points):
            # if a new reroute task has been started, exit this one
            if threading.get_ident() != self.reroute_thread.ident:
                print("Reroute task cancelled")
                return

            lat = point["lat"]
            lng = point["lng"]
            alt = point["alt"]

            gps_data = self.get_location()
            current_location = Location(gps_data['lat'], gps_data['lng'], gps_data['alt'])

            target_location = Location(lat, lng, alt)
            sharp_turn = get_degrees_needed_to_turn(self.get_heading(), current_location, target_location) > 80


            overShootLocation = get_point_further_away(current_location, target_location, 40)
            overshoot_lat = overShootLocation.lat
            overshoot_lng = overShootLocation.lng
            overshoot_alt = overShootLocation.alt

            print("Rerouting to : " + str(target_location))

            # if the current point is the last point or a sharpturn, fly to that location, otherwise overshoot
            # if index == len(points) - 1 or sharp_turn:
            #     self.fly_to(target_location.lat, target_location.lng, target_location.alt)
            # else:
            #     self.fly_to(overshoot_lat, overshoot_lng, overshoot_alt)

            self.fly_to(target_location.lat, target_location.lng, target_location.alt)

            while True: #!!! TO-DO Change True to while vehicle is in guided mode
                # if a new reroute task has been started, exit this one
                if threading.get_ident() != self.reroute_thread.ident:
                    print("Reroute task cancelled")
                    return

                self.telemetry.wait('GPS_RAW_INT')
                current_location = Location(self.telemetry.lat, self.telemetry.lng, self.telemetry.alt)

                remainingDistance = get_distance_metres(current_location, target_location)
                print("Distance to target: " + str(remainingDistance))
                if remainingDistance <= 1: #Just below target, in case of undershoot.
                    print("Reached waypoint")
                    break
        self.set_auto()

vehicle = Vehicle()
