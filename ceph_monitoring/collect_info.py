import re
import sys
import time
import json
import Queue
import shutil
import logging
import os.path
import argparse
import warnings
import threading
import subprocess


logger = logging.getLogger('collect')


class CollectSettings(object):
    def __init__(self):
        self.disabled = []

    def disable(self, pattern):
        self.disabled.append(re.compile(pattern))

    def allowed(self, path):
        for pattern in self.disabled:
            if pattern.search(path):
                return False
        return True


def color_me(color):
    RESET_SEQ = "\033[0m"
    COLOR_SEQ = "\033[1;%dm"

    color_seq = COLOR_SEQ % (30 + color)

    def closure(msg):
        return color_seq + msg + RESET_SEQ
    return closure


class ColoredFormatter(logging.Formatter):
    BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

    colors = {
        'WARNING': color_me(YELLOW),
        'DEBUG': color_me(BLUE),
        'CRITICAL': color_me(YELLOW),
        'ERROR': color_me(RED)
    }

    def __init__(self, msg, use_color=True, datefmt=None):
        logging.Formatter.__init__(self, msg, datefmt=datefmt)
        self.use_color = use_color

    def format(self, record):
        orig = record.__dict__
        record.__dict__ = record.__dict__.copy()
        levelname = record.levelname

        prn_name = levelname
        if levelname in self.colors:
            record.levelname = self.colors[levelname](prn_name)
        else:
            record.levelname = prn_name

        # super doesn't work here in 2.6 O_o
        res = logging.Formatter.format(self, record)

        # res = super(ColoredFormatter, self).format(record)

        # restore record, as it will be used by other formatters
        record.__dict__ = orig
        return res


def setup_loggers(def_level=logging.INFO, log_fname=None):
    logger.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setLevel(def_level)

    log_format = '%(asctime)s - %(levelname)s:%(name)s - %(message)s'
    colored_formatter = ColoredFormatter(log_format, datefmt="%H:%M:%S")

    sh.setFormatter(colored_formatter)
    logger.addHandler(sh)

    if log_fname is not None:
        fh = logging.FileHandler(log_fname)
        log_format = '%(asctime)s - %(levelname)8s:%(name)s - %(message)s'
        formatter = logging.Formatter(log_format, datefmt="%H:%M:%S")
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)
    else:
        fh = None


def check_output(cmd, log=True):
    if log:
        logger.debug("CMD: %r", cmd)

    p = subprocess.Popen(cmd, shell=True,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    out = p.communicate()
    code = p.wait()
    return code == 0, out[0]


def check_output_ssh(host, opts, cmd):
    logger.debug("SSH:%s: %r", host, cmd)
    ssh_opts = "-o LogLevel=quiet -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    return check_output("ssh {2} {0} {1}".format(host, cmd, ssh_opts), False)


class CephDataCollector(object):

    node_commands = [
        ("lshw", "xml", "lshw -xml"),
        ("lsblk", "txt", "lsblk -a"),
        ("diskstats", "txt", "cat /proc/diskstats"),
        ("uname", "txt", "uname -a"),
        ("dmidecode", "txt", "dmidecode"),
        ("meminfo", "txt", "cat /proc/meminfo"),
        ("loadavg", "txt", "cat /proc/loadavg"),
        ("cpuinfo", "txt", "cat /proc/cpuinfo"),
        ("mount", "txt", "mount"),
        ("ipa", "txt", "ip a")
    ]

    def __init__(self, opts, collect_settings, res_q=None):
        self.collect_settings = collect_settings
        self.opts = opts
        if res_q is None:
            self.res_q = Queue.Queue()
        else:
            self.res_q = res_q
        self.ceph_cmd = "ceph -c {0.conf} -k {0.key} --format json ".format(opts)

    def get_mon_hosts(self):
        ok, res = check_output(self.ceph_cmd + "mon_status")
        assert ok
        return [str(node['name']) for node in json.loads(res)['monmap']['mons']]

    def get_osd_hosts(self):
        ok, res = check_output(self.ceph_cmd + "osd tree")
        assert ok
        for node in json.loads(res)['nodes']:
            if node['type'] == 'host':
                for osd_id in node['children']:
                    yield str(node['name']), osd_id

    def run2emit(self, path, format, cmd, check=True):
        if check:
            if not self.collect_settings.allowed(path):
                return False, "--disabled--"

        ok, out = check_output(cmd)
        self.res_q.put((ok, path, (format if ok else 'txt'), out))
        return ok, out

    def ssh2emit(self, host, path, format, cmd, check=True):
        if check:
            if not self.collect_settings.allowed(path):
                return False, "--disabled--"

        ok, out = check_output_ssh(host, self.opts, cmd)
        self.res_q.put((ok, path, (format if ok else 'err'), out))
        return ok, out

    def collect_master_data(self, path=""):
        path = path + "/master/"

        for cmd in ['osd tree', 'pg dump', 'df', 'auth list',
                    'health', 'health detail', "mon_status",
                    'status']:
            self.run2emit(path + cmd.replace(" ", "_"), 'json',
                          self.ceph_cmd + cmd)

        self.run2emit(path + "rados_df", 'json', "rados df --format json")
        ok, lspools = self.run2emit(path + "osd_lspools", 'json',
                                    self.ceph_cmd + "osd lspools")
        assert ok

        pool_stats = {}
        for pool in json.loads(lspools):
            pool_name = pool['poolname']
            pool_stats[pool_name] = {}
            for stat in ['size', 'min_size', 'crush_ruleset']:
                ok, val = check_output(self.ceph_cmd + "osd pool get {0} {1}".format(pool_name, stat))
                assert ok
                pool_stats[pool_name][stat] = json.loads(val)[stat]

        self.res_q.put((True, path + 'pool_stats', 'json', json.dumps(pool_stats)))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out_file = os.tempnam()
            ok, out = check_output(self.ceph_cmd + "osd getcrushmap -o " + out_file)

            if not ok:
                self.res_q.put((ok, path + 'crushmap', 'err', out))
            else:
                data = open(out_file, "rb").read()
                os.unlink(out_file)
                self.res_q.put((ok, path + 'crushmap', 'bin', data))

    def collect_device_info(self, host, path, device_file):
        ok, dev_str = check_output_ssh(host, self.opts, "df " + device_file)
        assert ok

        dev_str = dev_str.strip()
        dev_link = dev_str.strip().split("\n")[1].split()[0]

        if dev_link == 'udev':
            dev_link = device_file

        used = int(dev_str.strip().split("\n")[1].split()[2]) * 1024
        avail = int(dev_str.strip().split("\n")[1].split()[3]) * 1024

        abs_path_cmd = '\'path="{0}" ;'.format(dev_link)
        abs_path_cmd += 'while [ -h "$path" ] ; do path=$(readlink "$path") ;'
        abs_path_cmd += ' path=$(readlink -f "$path") ; done ; echo $path\''
        ok, dev = check_output_ssh(host, self.opts, abs_path_cmd)
        assert ok

        root_dev = dev = dev.strip()
        while root_dev[-1].isdigit():
            root_dev = root_dev[:-1]

        cmd = "cat /sys/block/{0}/queue/rotational".format(os.path.basename(root_dev))
        ok, is_ssd_str = check_output_ssh(host, self.opts, cmd)
        assert ok
        is_ssd = is_ssd_str.strip() == '0'

        self.ssh2emit(host, path + '/hdparm', 'txt', "sudo hdparm -I " + root_dev)
        self.ssh2emit(host, path + '/smartctl', 'txt', "sudo smartctl -a " + root_dev)
        self.res_q.put((True, path + '/stats', 'json',
                        json.dumps({'dev': dev,
                                    'root_dev': root_dev,
                                    'used': used,
                                    'avail': avail,
                                    'is_ssd': is_ssd})))
        return dev

    def collect_osd_data(self, host, osd_id, path=""):
        path = "{0}/osd/{1}/".format(path, osd_id)
        osd_cfg_cmd = "sudo ceph -f json --admin-daemon /var/run/ceph/ceph-osd.{0}.asok config show"
        ok, data = self.ssh2emit(host, path + "config", 'json', osd_cfg_cmd.format(osd_id))
        assert ok

        osd_cfg = json.loads(data)
        self.collect_device_info(host, path + "journal", str(osd_cfg['osd_journal']))
        self.collect_device_info(host, path + "data", str(osd_cfg['osd_data']))
        self.ssh2emit(host, path + "osd_daemons", 'txt', "ps aux | grep ceph-osd")

    def collect_node_info(self, host):
        path = 'hosts/' + host + '/'
        for path_off, frmt, cmd in self.node_commands:
            self.ssh2emit(host, path + path_off, frmt, cmd)

        # self.ssh2emit(host, path + "vmstat", "txt",
        #               "vmstat 1 {0}".format(self.opts.stat_collect_seconds))
        # self.ssh2emit(host, path + "iostat", "txt",
        #               "iostat -x 1 {0}".format(self.opts.stat_collect_seconds))
        # self.ssh2emit(host, path + "top", "txt",
        #               "top -b -d {0} -n 10".format(self.opts.stat_collect_seconds))

    def collect_mon_data(self, host, path=""):
        path = "{0}/mon/{1}/".format(path, host)
        # osd_cfg_cmd = "sudo ceph -f json --admin-daemon /var/run/ceph/ceph-osd.{0}.asok config show"
        # ok, data = self.ssh2emit(host, path + "config", 'json', osd_cfg_cmd.format(osd_id))
        self.ssh2emit(host, path + "mon_daemons", 'txt', "ps aux | grep ceph-mon")

    def collect_all(self):
        q = Queue.Queue()
        q.put((self.collect_master_data,))

        all_hosts = set()
        osd_hosts = list(self.get_osd_hosts())
        logger.info("Found %s osd hosts", len(osd_hosts))

        for host, osd_id in osd_hosts:
            q.put((self.collect_osd_data, host, osd_id))
            logger.debug("Find OSD %s on host %s", osd_id, host)
            if host not in all_hosts:
                all_hosts.add(host)
                q.put((self.collect_node_info, host))

        mon_hosts = list(self.get_mon_hosts())
        logger.info("Found %s mon hosts", len(mon_hosts))

        for host in mon_hosts:
            q.put((self.collect_mon_data, host))
            logger.debug("Find mon on host %s", host)
            if host not in all_hosts:
                all_hosts.add(host)
                q.put((self.collect_node_info, host))

        def pool_thread():
            val = q.get()
            while val is not None:
                val[0](*val[1:])
                val = q.get()

        running_threads = []
        for i in range(self.opts.pool_size):
            th = threading.Thread(target=pool_thread)
            th.daemon = True
            th.start()
            running_threads.append(th)
            q.put(None)

        while True:
            time.sleep(0.01)
            if all(not th.is_alive() for th in running_threads):
                break


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("-c", "--conf",
                   default="/etc/ceph/ceph.conf",
                   help="Ceph cluster config file")

    p.add_argument("-k", "--key",
                   default="/etc/ceph/ceph.client.admin.keyring",
                   help="Ceph cluster key file")

    p.add_argument("-l", "--log-level",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                   default="INFO",
                   help="Colsole log level")

    p.add_argument("-p", "--pool-size",
                   default=64, type=int,
                   help="Worker pool size")

    p.add_argument("-s", "--stat-collect-seconds",
                   default=15, type=int, metavar="SEC",
                   help="Collect stats from node for SEC seconds")

    p.add_argument("-d", "--disable", default=[],
                   nargs='*', help="Disable collect pattern")

    p.add_argument("-r", "--result", default=None, help="Result file")

    p.add_argument("-k", "--keep-folder", default=False,
                   action="store_true",
                   help="Keep unpacked data")

    p.add_argument("-j", "--no-pretty-json", default=False,
                   action="store_true",
                   help="Don't prettify json data")

    return p.parse_args(argv[1:])


def main(argv):
    if not check_output('which ceph')[0]:
        logger.error("No 'ceph' command available. Run this script from node, which has ceph access")
        return

    # TODO: Logs from down OSD
    opts = parse_args(argv)
    res_q = Queue.Queue()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out_folder = os.tempnam()

    os.makedirs(out_folder)

    setup_loggers(def_level=getattr(logging, opts.log_level),
                  log_fname=os.path.join(out_folder, "log.txt"))

    collector_settings = CollectSettings()
    map(collector_settings.disable, opts.disable)
    collector = CephDataCollector(opts, collector_settings, res_q)

    th = threading.Thread(target=collector.collect_all)
    th.daemon = True
    th.start()

    while True:
        while res_q.empty() and th.is_alive():
            time.sleep(0.01)

        if not th.is_alive():
            if res_q.empty():
                break

        ok, path, frmt, out = res_q.get()

        while '//' in path:
            path.replace('//', '/')

        while path.startswith('/'):
            path = path[1:]

        while path.endswith('/'):
            path = path[:-1]

        fname = os.path.join(out_folder, path + '.' + frmt)
        dr = os.path.dirname(fname)

        if not os.path.exists(dr):
            os.makedirs(dr)

        if frmt == 'bin':
            open(fname, "wb").write(out)
        elif frmt == json:
            if not opts.no_pretty_json:
                out = json.dumps(json.loads(out), indent=4, sort_keys=True)
            open(fname, "wb").write(out)
        else:
            open(fname, "w").write(out)

    th.join()

    if opts.result is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out_file = os.tempnam() + ".tar.gz"
    else:
        out_file = opts.result

    check_output("cd {0} ; tar -zcvf {1} *".format(out_folder, out_file))
    logger.info("Result saved into %r", out_file)

    if opts.keep_folder:
        shutil.rmtree(out_folder)
    else:
        logger.info("Temporary folder %r", out_folder)

if __name__ == "__main__":
    exit(main(sys.argv))
