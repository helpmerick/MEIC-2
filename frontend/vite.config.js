import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// The panel binds localhost only (NFR-06). In dev, proxy API calls to the
// FastAPI backend on :8000 so requests are same-origin from the browser's view.
export default defineConfig({
    plugins: [react()],
    server: {
        host: "127.0.0.1",
        port: 5173,
        proxy: {
            "/state": "http://127.0.0.1:8000",
            "/report": "http://127.0.0.1:8000",
            "/arm": "http://127.0.0.1:8000",
            "/disarm": "http://127.0.0.1:8000",
            "/stop-trading": "http://127.0.0.1:8000",
            "/confirm-live": "http://127.0.0.1:8000",
            "/config": "http://127.0.0.1:8000",
            "/ws": { target: "ws://127.0.0.1:8000", ws: true },
        },
    },
});
