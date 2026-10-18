/**
 * LiDAR capture — native-module boundary.
 *
 * Expo has no managed LiDAR API. On iPhone Pro / iPad Pro the sensor is exposed
 * through ARKit's `ARSceneReconstruction` (ARMeshAnchor). Wiring it up means:
 *
 *   1. `npx create-expo-module --local lidar-capture` → a local Expo Module.
 *   2. In Swift, run an `ARSession` with `sceneReconstruction = .mesh`, accumulate
 *      `ARMeshAnchor` geometry, and export to .obj / .ply in the app sandbox.
 *   3. Expose `isAvailable()` and `captureMesh(durationMs)` to JS.
 *   4. Build a dev client (`npx expo run:ios`) — Expo Go cannot load it.
 *
 * Until that module exists, this stub reports LiDAR as unavailable so the
 * capture screen stays usable with photos only.
 */

import { Platform } from 'react-native';

export interface LidarCapture {
  uri: string;          // file:// path to the exported mesh
  format: 'obj' | 'ply';
  vertexCount: number;
  durationMs: number;
}

let nativeModule: {
  isAvailable: () => Promise<boolean>;
  captureMesh: (opts: { outputDir: string; durationMs: number }) => Promise<LidarCapture>;
} | null = null;

try {
  // Resolved only when the local module is present in the dev client.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  nativeModule = require('lidar-capture');
} catch {
  nativeModule = null;
}

export async function lidarAvailable(): Promise<boolean> {
  if (Platform.OS !== 'ios' || !nativeModule) return false;
  try {
    return await nativeModule.isAvailable();
  } catch {
    return false;
  }
}

export async function captureLidarMesh(outputDir: string, durationMs = 8000): Promise<LidarCapture | null> {
  if (!(await lidarAvailable()) || !nativeModule) return null;
  return nativeModule.captureMesh({ outputDir, durationMs });
}
