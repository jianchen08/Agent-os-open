#!/usr/bin/env node

// Simple test script to verify search functionality
import { SearchEngine } from '../dist/search-engine.js';

const searchEngine = new SearchEngine();

async function testSearch() {
  console.log('Testing search functionality...');
  
  try {
    const result = await searchEngine.search({
      query: 'test search',
      numResults: 3,
      timeout: 15000  // 15 second timeout for testing
    });
    
    console.log(`Search completed with engine: ${result.engine}`);
    console.log(`Found ${result.results.length} results:`);
    
    result.results.forEach((r, i) => {
      console.log(`${i + 1}. ${r.title}`);
      console.log(`   URL: ${r.url}`);
      console.log(`   Description: ${r.description.substring(0, 100)}...`);
      console.log('');
    });
    
    // Clean up
    await searchEngine.closeAll();
    
  } catch (error) {
    console.error('Search test failed:', error);
    await searchEngine.closeAll();
    process.exit(1);
  }
}

testSearch().then(() => {
  console.log('Test completed successfully');
  process.exit(0);
});