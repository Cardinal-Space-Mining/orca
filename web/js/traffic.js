// Recent traffic per machine interface, for the network page's sparklines.
// Fed from every "machine" event (Orca adds rx_bps/tx_bps per
// interface).

const MAX_POINTS = 90;

class TrafficStore {
    constructor() {
        this.series = new Map(); // "machine/interface" -> [[time_s, bps]]
    }

    add(machineEntry) {
        const stats = machineEntry.stats;
        if (!stats) return;
        for (const entry of stats.interfaces || []) {
            if (entry.rx_bps == null) continue;
            const key = `${machineEntry.machine.id}/${entry.name}`;
            const points = this.series.get(key) || [];
            const last = points[points.length - 1];
            if (last && last[0] === stats.time_s) continue;
            points.push([stats.time_s, entry.rx_bps + entry.tx_bps]);
            if (points.length > MAX_POINTS) points.shift();
            this.series.set(key, points);
        }
    }

    points(machineId, interfaceName) {
        return this.series.get(`${machineId}/${interfaceName}`) || [];
    }
}

export const trafficStore = new TrafficStore();
