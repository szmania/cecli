#!/usr/bin/env node

/**
 * filter-chat-mode.js - Filter JSON entries to keep only those with "mode": "chat"
 * 
 * Usage:
 *   node filter-chat-mode.js <input-file> [output-file]
 *   cat input.json | node filter-chat-mode.js > output.json
 * 
 * The script expects JSON input that is either:
 * - An array of objects, each potentially having a "mode" field
 * - An object where values are objects with a "mode" field
 * 
 * It will filter to keep only entries where "mode" === "chat"
 */

const fs = require('fs');
const path = require('path');

function filterChatMode(data) {
    if (Array.isArray(data)) {
        // If input is an array, filter objects with mode: "chat"
        return data.filter(item => 
            item && typeof item === 'object' && item.mode === 'chat'
        );
    } else if (data && typeof data === 'object') {
        // If input is an object, filter properties with mode: "chat"
        const result = {};
        for (const [key, value] of Object.entries(data)) {
            if (value && typeof value === 'object' && value.mode === 'chat') {
                result[key] = value;
            }
        }
        return result;
    } else {
        throw new Error('Input must be a JSON array or object');
    }
}

function main() {
    let inputData;
    let inputPath = null;
    let outputPath = null;

    // Parse command line arguments
    const args = process.argv.slice(2);
    
    if (args.length > 0) {
        // First argument is input file
        inputPath = args[0];
        try {
            const inputContent = fs.readFileSync(inputPath, 'utf8');
            inputData = JSON.parse(inputContent);
        } catch (error) {
            console.error(`Error reading or parsing ${inputPath}:`, error.message);
            process.exit(1);
        }
        
        // Second argument (optional) is output file
        if (args.length > 1) {
            outputPath = args[1];
        } else {
            // If only input file is provided, overwrite it in place
            outputPath = inputPath;
        }
    } else {
        // Read from stdin
        try {
            const stdinContent = fs.readFileSync(0, 'utf8'); // 0 = stdin
            inputData = JSON.parse(stdinContent);
        } catch (error) {
            console.error('Error reading or parsing stdin:', error.message);
            console.error('\nUsage:');
            console.error('  node filter-chat-mode.js <input-file> [output-file]');
            console.error('  cat input.json | node filter-chat-mode.js > output.json');
            process.exit(1);
        }
    }

    try {
        const filteredData = filterChatMode(inputData);
        
        const outputJson = JSON.stringify(filteredData, null, 2);
        
        if (outputPath) {
            fs.writeFileSync(outputPath, outputJson, 'utf8');
            if (outputPath === inputPath) {
                console.error(`Filtered data written back to ${inputPath}`);
            } else {
                console.error(`Filtered data written to ${outputPath}`);
            }
        } else {
            console.log(outputJson);
        }
    } catch (error) {
        console.error('Error filtering data:', error.message);
        process.exit(1);
    }
}

if (require.main === module) {
    main();
}

module.exports = { filterChatMode };
