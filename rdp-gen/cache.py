# cache.py
import time
import threading

class ProxmoxCache:
    def __init__(self, client, ttl=5):
        self.client = client
        self.ttl = ttl
        self.lock = threading.Lock()
        self.last = 0
        self.snapshot = {
            "vms": [],
        }

    def _refresh(self):
        # 1) bulk node list
        nodes = self.client.get_nodes()

        # 2) bulk VM list per node
        vms = []
        for n in nodes:
            node = n["node"]
            vms.extend(self.client.list_qemu(node))

        # 3) optional: fetch statuses only (cheap)
        for vm in vms:
            node = vm["node"]
            vmid = vm["vmid"]

            try:
                st = self.client.status_qemu(node, vmid)
                vm["status"] = st.get("status", "unknown")
            except:
                vm["status"] = "unknown"

        self.snapshot = {"vms": vms}
        self.last = time.time()

    def get(self):
        now = time.time()
        with self.lock:
            if now - self.last > self.ttl:
                self._refresh()
            return self.snapshot
