#!/usr/bin/env node
/**
 * Playwright script for automating Jupyter Lab setup in Google Colab
 * 
 * This script:
 * 1. Opens the Colab notebook URL
 * 2. Presses Ctrl+F9 to execute the first cell (starts Jupyter Lab)
 * 3. Waits for a new tab to open with Jupyter Labs
 * 4. Opens another notebook in Jupyter Labs
 * 5. Presses Shift+Enter twice to execute cells
 */

const { chromium } = require('playwright');

(async () => {
    // Launch browser
    const browser = await chromium.launch({
        headless: false, // Set to true for headless mode
        args: ['--start-maximized']
    });

    const context = await browser.newContext({
        viewport: { width: 1920, height: 1080 }
    });

    const page = await context.newPage();

    try {
        // Step 1: Open the Colab notebook
        console.log('Opening Colab notebook...');
        await page.goto('https://colab.research.google.com/github/scatterp2/webvm/blob/main/jupyter%20(1).ipynb', {
            waitUntil: 'networkidle',
            timeout: 60000
        });

        // Wait for the notebook to fully load
        console.log('Waiting for notebook to load...');
        await page.waitForSelector('colab-toolbar-button', { timeout: 30000 });
        await new Promise(resolve => setTimeout(resolve, 5000)); // Additional wait for UI stability

        // Step 2: Press Ctrl+F9 to execute the current cell
        console.log('Pressing Ctrl+F9 to execute cell...');
        await page.keyboard.press('Control+F9');

        // Step 3: Wait for Jupyter Lab to start and a new tab/window to open
        console.log('Waiting for Jupyter Lab URL to appear...');
        
        // Wait for the proxy URL to be displayed in the output
        await page.waitForSelector('colab-output-cell', { timeout: 60000 });
        
        // Extract the Jupyter Lab URL from the page
        const urlText = await page.evaluate(() => {
            const outputs = document.querySelectorAll('colab-output-cell');
            for (let output of outputs) {
                const text = output.textContent;
                // Look for URLs starting with https:// and containing the port number
                const match = text.match(/https:\/\/[^\s]+(?::\d+)?\/?[^\s]*/);
                if (match && match[0].includes('colab.dev')) {
                    return match[0];
                }
            }
            return null;
        });

        if (urlText) {
            console.log(`Found Jupyter Lab URL: ${urlText}`);
            
            // Step 4: Open Jupyter Lab in a new tab
            console.log('Opening Jupyter Lab in new tab...');
            const jupyterPage = await context.newPage();
            await jupyterPage.goto(urlText, {
                waitUntil: 'networkidle',
                timeout: 60000
            });

            // Wait for Jupyter Lab interface to load
            console.log('Waiting for Jupyter Lab interface...');
            await jupyterPage.waitForSelector('[data-testid="fileBrowser"], .jp-Launcher', { timeout: 60000 });
            await new Promise(resolve => setTimeout(resolve, 3000));

            // Step 5: Open a notebook in Jupyter Lab
            console.log('Opening notebook in Jupyter Lab...');
            
            // Click on the Python 3 kernel to create a new notebook
            try {
                await jupyterPage.click('text=Python 3 (ipykernel)', { timeout: 10000 });
            } catch (e) {
                // Alternative: click on the notebook icon in launcher
                await jupyterPage.click('[data-command="notebook:create-new"]', { timeout: 10000 });
            }

            // Wait for the notebook to open
            await jupyterPage.waitForSelector('.jp-Notebook', { timeout: 30000 });
            await new Promise(resolve => setTimeout(resolve, 2000));

            // Step 6: Press Shift+Enter twice to execute cells
            console.log('Pressing Shift+Enter twice...');
            await jupyterPage.keyboard.press('Shift+Enter');
            await new Promise(resolve => setTimeout(resolve, 500));
            await jupyterPage.keyboard.press('Shift+Enter');

            console.log('Automation complete! Jupyter Lab is running with executed notebook.');
            
            // Keep the browser open for inspection
            console.log('Browser will remain open for 5 minutes for inspection...');
            await new Promise(resolve => setTimeout(resolve, 300000));
        } else {
            console.error('Could not find Jupyter Lab URL in the output');
            // Take a screenshot for debugging
            await page.screenshot({ path: 'colab-error.png' });
        }

    } catch (error) {
        console.error('Error during automation:', error);
        await page.screenshot({ path: 'automation-error.png' });
    } finally {
        // Close browser after completion or error
        await browser.close();
    }
})();
