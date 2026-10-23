// Local-only PostgreSQL/WASM + pgvector for development.
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { PGLiteSocketServer } from '@electric-sql/pglite-socket';

const directory = resolve('.storage/pglite');
await mkdir(directory, { recursive: true });
const db = await PGlite.create({ dataDir: directory, extensions: { vector } });
await db.exec('CREATE EXTENSION IF NOT EXISTS vector;');
const server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 55432 });
await server.start();
console.log('Local-only PostgreSQL/WASM + pgvector listening on 127.0.0.1:55432');
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, async () => { await server.stop(); await db.close(); process.exit(0); });
}
