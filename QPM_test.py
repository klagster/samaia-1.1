#!/usr/bin/env python3
"""
Compare performance: Raw vs Rate-Limited requests
Tests both approaches to see which is faster and more reliable
"""

import os
import asyncio
import logging
import time
from google import genai
from google.genai.types import HttpOptions
from google.oauth2 import service_account
from google.genai.errors import APIError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------------
# Token Bucket Rate Limiter
# ---------------------------
class TokenBucketRateLimiter:
    """Token bucket rate limiter for smooth request distribution."""
    
    def __init__(self, requests_per_minute: int):
        self.requests_per_minute = requests_per_minute
        self.tokens = float(requests_per_minute)
        self.max_tokens = float(requests_per_minute)
        self.last_update = time.time()
        self.lock = asyncio.Lock()
        self.refill_rate = requests_per_minute / 60.0
    
    async def acquire(self):
        """Acquire a token, waiting if necessary."""
        async with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.max_tokens, self.tokens + (elapsed * self.refill_rate))
            self.last_update = now
            
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.refill_rate
                await asyncio.sleep(wait_time)
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.max_tokens, self.tokens + (elapsed * self.refill_rate))
                self.last_update = now
            
            self.tokens -= 1.0

async def test_raw_speed(client, num_requests=50):
    """Test 1: Raw speed with no rate limiting (will hit 429s)"""
    
    logger.info("=" * 70)
    logger.info("TEST 1: RAW SPEED (No Rate Limiting)")
    logger.info("=" * 70)
    logger.info(f"Making {num_requests} requests as fast as possible...\n")
    
    start_time = time.time()
    success_count = 0
    error_count = 0
    first_429_at = None
    
    for i in range(num_requests):
        try:
            resp = await client.aio.models.generate_content(
                model=os.getenv("MODEL_SEARCH", "gemini-2.5-pro"),
                contents=[{"role": "user", "parts": [{"text": f"{i}"}]}],
                config={"max_output_tokens": 5}
            )
            success_count += 1
            
            if (i + 1) % 10 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed * 60
                logger.info(f"   ✅ {i+1}/{num_requests} - Rate: {rate:.0f} QPM")
            
        except APIError as e:
            error_count += 1
            if first_429_at is None and '429' in str(e):
                first_429_at = i + 1
                elapsed = time.time() - start_time
                logger.warning(f"   🚫 First 429 at request #{first_429_at} ({elapsed:.1f}s)")
        
        await asyncio.sleep(0.01)  # Tiny delay
    
    elapsed_time = time.time() - start_time
    
    logger.info("\n" + "─" * 70)
    logger.info("TEST 1 RESULTS:")
    logger.info(f"   Successful: {success_count}/{num_requests}")
    logger.info(f"   Failed (429): {error_count}")
    logger.info(f"   Time: {elapsed_time:.2f}s")
    logger.info(f"   Effective rate: {(success_count/elapsed_time)*60:.1f} QPM")
    logger.info(f"   First 429 at: Request #{first_429_at}" if first_429_at else "   No 429s!")
    logger.info("=" * 70 + "\n")
    
    return {
        'success': success_count,
        'errors': error_count,
        'time': elapsed_time,
        'first_429': first_429_at
    }

async def test_rate_limited_speed(client, num_requests=50, qpm=57, concurrency=3):
    """Test 2: Rate-limited with concurrency"""
    
    logger.info("=" * 70)
    logger.info(f"TEST 2: RATE LIMITED ({qpm} QPM, {concurrency} concurrent)")
    logger.info("=" * 70)
    logger.info(f"Making {num_requests} requests with rate limiting...\n")
    
    rate_limiter = TokenBucketRateLimiter(requests_per_minute=qpm)
    semaphore = asyncio.Semaphore(concurrency)
    
    async def make_request(i):
        async with semaphore:
            await rate_limiter.acquire()
            try:
                resp = await client.aio.models.generate_content(
                    model=os.getenv("MODEL_SEARCH", "gemini-2.5-pro"),
                    contents=[{"role": "user", "parts": [{"text": f"{i}"}]}],
                    config={"max_output_tokens": 5}
                )
                return {'success': True, 'index': i}
            except APIError as e:
                return {'success': False, 'index': i, 'error': str(e)}
    
    start_time = time.time()
    
    # Create all tasks
    tasks = [make_request(i) for i in range(num_requests)]
    
    # Execute with progress updates
    results = []
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        result = await coro
        results.append(result)
        
        if (i + 1) % 10 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed * 60
            logger.info(f"   ✅ {i+1}/{num_requests} - Rate: {rate:.1f} QPM")
    
    elapsed_time = time.time() - start_time
    
    success_count = sum(1 for r in results if r['success'])
    error_count = sum(1 for r in results if not r['success'])
    first_429 = next((r['index'] + 1 for r in results if not r['success'] and '429' in r.get('error', '')), None)
    
    logger.info("\n" + "─" * 70)
    logger.info("TEST 2 RESULTS:")
    logger.info(f"   Successful: {success_count}/{num_requests}")
    logger.info(f"   Failed (429): {error_count}")
    logger.info(f"   Time: {elapsed_time:.2f}s")
    logger.info(f"   Effective rate: {(success_count/elapsed_time)*60:.1f} QPM")
    logger.info(f"   First 429 at: Request #{first_429}" if first_429 else "   No 429s!")
    logger.info("=" * 70 + "\n")
    
    return {
        'success': success_count,
        'errors': error_count,
        'time': elapsed_time,
        'first_429': first_429
    }

async def compare_performance():
    """Run both tests and compare results."""
    
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEX_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    
    logger.info("=" * 70)
    logger.info("🏁 PERFORMANCE COMPARISON TEST")
    logger.info("=" * 70)
    logger.info(f"   Project: {project}")
    logger.info(f"   Testing 50 requests with each approach")
    logger.info("=" * 70 + "\n")
    
    # Create client
    client = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=HttpOptions(
            api_version="v1",
            headers={
                "X-Vertex-AI-LLM-Request-Type": "dedicated",
            },
        ),
    )
    
    # Run Test 1: Raw speed
    raw_results = await test_raw_speed(client, num_requests=50)
    
    # Wait a bit between tests
    logger.info("⏳ Waiting 10 seconds before next test...\n")
    await asyncio.sleep(10)
    
    # Run Test 2: Rate limited (moderate settings)
    limited_results = await test_rate_limited_speed(client, num_requests=50, qpm=57, concurrency=3)
    
    # Comparison
    logger.info("=" * 70)
    logger.info("📊 COMPARISON SUMMARY")
    logger.info("=" * 70)
    
    logger.info("\nRAW SPEED:")
    logger.info(f"   Success Rate: {raw_results['success']}/50 ({raw_results['success']/50*100:.0f}%)")
    logger.info(f"   Time: {raw_results['time']:.2f}s")
    logger.info(f"   Errors: {raw_results['errors']}")
    
    logger.info("\nRATE LIMITED:")
    logger.info(f"   Success Rate: {limited_results['success']}/50 ({limited_results['success']/50*100:.0f}%)")
    logger.info(f"   Time: {limited_results['time']:.2f}s")
    logger.info(f"   Errors: {limited_results['errors']}")
    
    logger.info("\nANALYSIS:")
    time_diff = limited_results['time'] - raw_results['time']
    time_diff_pct = (time_diff / raw_results['time']) * 100
    
    if limited_results['errors'] < raw_results['errors']:
        logger.info(f"   ✅ Rate limiting eliminated {raw_results['errors'] - limited_results['errors']} errors")
    
    if limited_results['success'] > raw_results['success']:
        logger.info(f"   ✅ Rate limiting completed {limited_results['success'] - raw_results['success']} more requests")
    
    logger.info(f"   ⏱️  Rate limiting was {abs(time_diff):.1f}s {'slower' if time_diff > 0 else 'faster'} ({abs(time_diff_pct):.1f}%)")
    
    if limited_results['errors'] == 0 and raw_results['errors'] > 0:
        logger.info(f"\n   🏆 WINNER: Rate Limited approach")
        logger.info(f"      - Zero errors")
        logger.info(f"      - Predictable performance")
        logger.info(f"      - More reliable for production")
    elif time_diff < 0:
        logger.info(f"\n   ⚡ Rate limiting is actually faster due to fewer retries!")
    else:
        logger.info(f"\n   💡 Rate limiting is slightly slower but much more reliable")
    
    logger.info("\n" + "=" * 70)
    
    # Test more aggressive settings
    logger.info("\n\n⚡ BONUS: Testing aggressive settings...")
    logger.info("⏳ Waiting 10 seconds...\n")
    await asyncio.sleep(10)
    
    aggressive_results = await test_rate_limited_speed(client, num_requests=50, qpm=61, concurrency=4)
    
    logger.info("=" * 70)
    logger.info("🚀 AGGRESSIVE SETTINGS TEST (61 QPM, 4 concurrent)")
    logger.info("=" * 70)
    logger.info(f"   Success Rate: {aggressive_results['success']}/50 ({aggressive_results['success']/50*100:.0f}%)")
    logger.info(f"   Time: {aggressive_results['time']:.2f}s")
    logger.info(f"   Errors: {aggressive_results['errors']}")
    
    if aggressive_results['errors'] == 0:
        logger.info(f"\n   ✅ AGGRESSIVE SETTINGS WORK! Consider using:")
        logger.info(f"      EFFECTIVE_QPM = 61")
        logger.info(f"      CONCURRENCY_LIMIT = 4")
    else:
        logger.info(f"\n   ⚠️  Stick with moderate settings (57 QPM, 3 concurrent)")
    
    logger.info("=" * 70)

if __name__ == "__main__":
    asyncio.run(compare_performance())