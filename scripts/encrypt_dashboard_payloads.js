#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");

const { subtle } = webcrypto;
const PROJECT_DIR = path.resolve(__dirname, "..");
const DASHBOARD_DIR = path.join(PROJECT_DIR, "dashboard");
const CONFIG_PATH = path.join(PROJECT_DIR, ".staticrypt.json");
const IV_BITS = 16 * 8;

const UTF8Encoder = {
  parse(str) {
    return new TextEncoder().encode(str);
  },
  stringify(bytes) {
    return new TextDecoder().decode(bytes);
  },
};

const HexEncoder = {
  parse(hexString) {
    if (hexString.length % 2 !== 0) throw new Error("Invalid hex string length");
    const bytes = new Uint8Array(hexString.length / 2);
    for (let i = 0; i < hexString.length; i += 2) {
      const byteValue = parseInt(hexString.slice(i, i + 2), 16);
      if (Number.isNaN(byteValue)) throw new Error("Invalid hex string");
      bytes[i / 2] = byteValue;
    }
    return bytes;
  },
  stringify(bytes) {
    return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  },
};

async function pbkdf2(password, salt, iterations, hashAlgorithm) {
  const key = await subtle.importKey("raw", UTF8Encoder.parse(password), "PBKDF2", false, ["deriveBits"]);
  const keyBytes = await subtle.deriveBits(
    {
      name: "PBKDF2",
      hash: hashAlgorithm,
      iterations,
      salt: UTF8Encoder.parse(salt),
    },
    key,
    256
  );
  return HexEncoder.stringify(new Uint8Array(keyBytes));
}

async function hashPassword(password, salt) {
  let hashed = await pbkdf2(password, salt, 1000, "SHA-1");
  hashed = await pbkdf2(hashed, salt, 14000, "SHA-256");
  return pbkdf2(hashed, salt, 585000, "SHA-256");
}

async function encrypt(plaintext, hashedPassword) {
  const iv = webcrypto.getRandomValues(new Uint8Array(IV_BITS / 8));
  const key = await subtle.importKey("raw", HexEncoder.parse(hashedPassword), "AES-CBC", false, ["encrypt"]);
  const encrypted = await subtle.encrypt({ name: "AES-CBC", iv }, key, UTF8Encoder.parse(plaintext));
  return HexEncoder.stringify(iv) + HexEncoder.stringify(new Uint8Array(encrypted));
}

async function encryptFile(sourceName, outputDir, hashedPassword, salt) {
  const inputPath = path.join(DASHBOARD_DIR, sourceName);
  const outputPath = path.join(outputDir, sourceName);
  const plaintext = fs.readFileSync(inputPath, "utf8");
  const ciphertext = await encrypt(plaintext, hashedPassword);
  const payload = {
    version: 1,
    encrypted: true,
    format: "staticrypt-aes-cbc",
    salt,
    generated_at: new Date().toISOString(),
    ciphertext,
  };
  fs.writeFileSync(outputPath, JSON.stringify(payload, null, 2));
}

async function main() {
  const [, , outputDir, ...files] = process.argv;
  if (!outputDir || !files.length) {
    throw new Error("Usage: encrypt_dashboard_payloads.js <output-dir> <file> [file...]");
  }
  const password = process.env.STATICRYPT_PASSWORD;
  if (!password) {
    throw new Error("STATICRYPT_PASSWORD is required");
  }
  const config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  if (!config.salt) {
    throw new Error(`Missing salt in ${CONFIG_PATH}`);
  }
  fs.mkdirSync(outputDir, { recursive: true });
  const hashedPassword = await hashPassword(password, String(config.salt));
  for (const file of files) {
    await encryptFile(file, outputDir, hashedPassword, String(config.salt));
  }
}

main().catch(err => {
  console.error(err.message);
  process.exit(1);
});
