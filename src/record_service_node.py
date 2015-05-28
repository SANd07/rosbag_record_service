#!/usr/bin/python

"""
This file defines the serving node for managing ROS records
"""

PKG = "rosbag_record_service"
import datetime
import roslib; roslib.load_manifest(PKG)
import os
import subprocess
from record_service.srv import *
from record_service.msg import *
import rospy
import signal
import yaml
from yaml.scanner import ScannerError


class ArgStruct:
    """
    Class to maintain options passed for each request.
    Constructs appropriate command strings.
    """
    def __init__(self):
        self.regex = ''
        self.exclude_regex = ''
        self.quiet = True
        self.output_prefix = ''
        self.output_folder = ''
        self.split_size = 0
        self.topics = list()
        self.all = False
        self.error = None
        self.compression = ''

    def command_string(self):
        """
        Generates a command string that represents appropriately the settings parsed
        """
        command_parts = ['rosbag record']
        if self.all:  # Then we record all topics
            command_parts.append('-a')
        if self.quiet:  # Then nothing is output to the console
            command_parts.append('-q')
        if self.regex != '':  # This is to include topics that match certain a certain regex
            command_parts.append('-e %s' % self.regex)
        if self.exclude_regex != '':  # This is to exclude certain topics
            command_parts.append('-x %s' % self.exclude_regex)
        if self.split_size > 0:  # Bag files are split into parts if they get too big
            command_parts.append('--split --size=%s' % self.split_size)
        if self.compression in ('bz2', 'lz4'): # Compression
            command_parts.append('--%s' % self.compression)
        output_folder = self.output_folder
        if output_folder == '':
            output_folder = '/tmp/'
        if output_folder[-1] != '/':
            output_folder += '/'
        output_folder = os.path.expanduser(output_folder)
        if not os.path.isdir(output_folder):
            self.error = "%s is not a valid directory" % output_folder
            print self.error
            return
        dt = datetime.datetime.now()
        date = dt.strftime("%Y-%m-%d")
        output_folder += date + '/'
        if not os.path.isdir(output_folder):
            os.mkdir(output_folder)  # Make this directory to group today's bags
        output_prefix = self.output_prefix
        if output_prefix == '':
            output_prefix = dt.strftime("%H-%M-%S")
        else:
            output_prefix = dt.strftime(output_prefix)
        command_parts.append('-o %s' % output_folder + output_prefix)
        if not self.all:
            command_parts.append(' '.join(self.topics))
        return ' '.join(command_parts)


class RecordServiceNode:
    """
    Node Service that can be called to start or stop recording certain ROS topics.
    Extends @rosbag
    """
    def __init__(self):
        self.bag_record_map = dict()  # This maintains a map of groups being recorded and their PIDs
        self.topic_groups = dict() # Maintains the arguments for each topic group loaded from config

        self.service_name = "record_service"
        rospy.init_node(self.service_name)
        self.load_config()
        self.publisher = rospy.Publisher("~status", RecordMsg, queue_size=1, latch=True)
        self.service = rospy.Service(self.service_name, RecordSrv, self.request_handler)
        self.publish_topics()
        rospy.spin()

    def publish_topics(self):
        """
        Publish the currently active topic groups
        """
        msg = RecordMsg()
        #msg.topics = self.bag_record_map.keys()
        msg.groups = self.topic_groups.keys()
        msg.statuses = [group in self.bag_record_map for group in msg.groups]
        self.publisher.publish(msg)

    def request_handler(self, request):
        """
        Handles all incoming requests to this service
        :param request: type(RecordSrvRequest) - (action, config_file, topic_group)
        :return: RecordSrvResponse - (return_code, output_message)
        """
        if request.action == request.START:
            if request.topic_group in self.bag_record_map:
                # This means there is already a process running for the same group. We don't need another process
                return RecordSrvResponse(return_code=RecordSrvResponse.ALREADY_RECORDING)

            # Check if this topic groups exists
            if request.topic_group not in self.topic_groups:
                # Topic group does not exist, return error
                return RecordSrvResponse(return_code=RecordSrvResponse.INVALID_GROUP)

            # Get arg struct for this topic group
            arg_struct = self.topic_groups[request.topic_group]

            if arg_struct.error is not None:
                # Then there is an error; return immediately
                return RecordSrvResponse(return_code=RecordSrvResponse.ERROR)

            # If we are here, then there was no error and we have to start another process for a group
            command = arg_struct.command_string()
            child_process = subprocess.Popen(command.split())
            self.bag_record_map[request.topic_group] = child_process
            self.publish_topics()
            return RecordSrvResponse(return_code=RecordSrvResponse.OK)

        elif request.action == request.STOP:
            if request.topic_group in self.bag_record_map:
                child_process = self.bag_record_map.pop(request.topic_group)
                self.kill_process_tree(child_process)
                self.publish_topics()
                return RecordSrvResponse(return_code=RecordSrvResponse.OK)
            else:
                return RecordSrvResponse(return_code=RecordSrvResponse.NOT_RUNNING)

        else:
            return RecordSrvResponse(return_code=RecordSrvResponse.INVALID_ACTION)

    @staticmethod
    def kill_process_tree(process):
        """
        Kills the process and all of its children
        :param process: A subprocess.Popen object
        :return:
        """
        ps_command = subprocess.Popen("ps -o pid --ppid %d --noheaders" % process.pid,
                                      shell=True, stdout=subprocess.PIPE)
        ps_output = ps_command.stdout.read()
        ps_command.wait()
        for pid_str in ps_output.split("\n")[:-1]:
            os.kill(int(pid_str), signal.SIGINT)
        process.terminate()
        process.wait()

    def load_config(self):
        """
        Loads the configuration from the ros parameter server and generates ArgStructs
        for each of them and places in the topic_groups dict.
        """
        output_folder = rospy.get_param('~output_folder','')
        split_size = rospy.get_param('~split_size',0)
        quiet = rospy.get_param('~quiet',True)
        exclude = rospy.get_param('~exclude','')
        compression = rospy.get_param('~compression','')

        for group_name, group_settings in rospy.get_param('~topic_groups', {}).items():
            arg_struct = ArgStruct()
            arg_struct.regex = group_settings.get('regex', '')
            arg_struct.all = group_settings.get('all', False)
            arg_struct.output_prefix = group_settings.get('output_prefix', '')
            arg_struct.topics = group_settings.get('topics', [])

            arg_struct.exclude_regex = group_settings.get('exclude', exclude)
            arg_struct.output_folder = group_settings.get('output_folder', output_folder)
            arg_struct.split_size = group_settings.get('split_size', split_size)
            arg_struct.quiet = group_settings.get('quiet', quiet)
            arg_struct.compression = group_settings.get('compression', compression)
            self.topic_groups[group_name] = arg_struct

    @staticmethod
    def parse_config(config_file, topic_group):
        """
        Parses the configuration file and generates an ArgStruct object that can be used
        to generate the command that records ROS topics
        :param config_file: absolute path to a YAML configuration file
        :param topic_group: One of the topics whose configuration can be found in the config file
        :return:
        """
        arg_struct = ArgStruct()
        try:
            c_file = open(config_file)
        except IOError as e:
            arg_struct.error = "%s: %s" % (config_file, e.strerror)
            return arg_struct
        try:
            doc = yaml.load(c_file)
        except ScannerError as e:
            arg_struct.error = "%s: %s" % (config_file, str(e))
            return arg_struct
        c_file.close()

        if topic_group not in doc:
            arg_struct.error = "\"%s\" isn't a valid topic group in the yaml file provided" % topic_group
            return arg_struct
        group_settings = doc[topic_group]

        arg_struct.regex = group_settings.get('regex', '')
        arg_struct.exclude_regex = group_settings.get('exclude', '')
        arg_struct.all = group_settings.get('all', False)
        arg_struct.output_folder = group_settings.get('output_folder', '')
        arg_struct.output_prefix = group_settings.get('output_prefix', '')
        arg_struct.split_size = group_settings.get('split_size', 0)
        arg_struct.quiet = group_settings.get('quiet', True)
        arg_struct.topics = group_settings.get('topics', [])

        return arg_struct


if __name__ == "__main__":
    n = RecordServiceNode()
