# Housing Affordability Tracker

A clean, modern dashboard displaying Hawaii housing affordability metrics across different counties.

## Features

- **Interactive Dashboard**: Toggle between Single Family Homes and Condos
- **Visual Data Representation**: Bar charts showing prices, affordability index, and payment-to-income ratios
- **Detailed Metrics Table**: Complete data breakdown by county
- **Responsive Design**: Works on desktop and mobile devices
- **Squarespace Ready**: Simple HTML/CSS/JavaScript implementation

## Data Included

### Counties Covered
- Hawaii (State)
- Honolulu
- Maui
- Hawaii
- Kauai

### Metrics Tracked
- Median resale prices
- Median family income
- Monthly mortgage payments (P&I)
- Payment-to-income share percentage
- Affordability Index (base = 100)
- Down payment amounts

## Installation for Squarespace

1. **Upload Files**:
   - Go to your Squarespace site
   - Navigate to Pages → Add Page → Blank Page
   - Add a Code Block to the page

2. **Add the Code**:
   - Copy the contents of `index.html`
   - Paste into the Code Block
   - In the Code Block settings, select "HTML"

3. **Add CSS**:
   - Go to Design → Custom CSS
   - Copy and paste the contents of `styles.css`

4. **Add JavaScript**:
   - Go to Settings → Advanced → Code Injection
   - Paste the contents of `script.js` wrapped in `<script>` tags into the Footer section:
   ```html
   <script>
   // Paste script.js contents here
   </script>
   ```

## Local Testing

To test locally, simply open `index.html` in a web browser. All files are self-contained and require no build process or dependencies.

## File Structure

```
Housing Affordability Tracker/
├── index.html          # Main HTML structure
├── styles.css          # Styling and layout
├── script.js           # Data and interactivity
└── README.md           # Documentation
```

## Customization

### Updating Data
Edit the `housingData` object in `script.js` to update values:

```javascript
const housingData = {
    sfh: {
        counties: [...],
        medianPrice: [...],
        // etc.
    }
}
```

### Changing Colors
Both `styles.css` and `index.html` use the same coastal palette tokens
declared at the top of each file. Override the `:root` custom properties
to retheme:

| Token              | Default     | Role                                  |
|--------------------|-------------|---------------------------------------|
| `--ocean-700`      | `#0b5566`   | Primary chrome, header background     |
| `--ocean-500`      | `#1a8496`   | Secondary chrome, links               |
| `--seafoam-700`    | `#1e8a73`   | Brand accent, "affordable" semantic   |
| `--seafoam-500`    | `#3fc4a0`   | Bar fills, success highlights         |
| `--coral-500`      | `#c94f3a`   | "Cost-burdened" / warning             |
| `--coral-300`      | `#e08a5b`   | Moderate / stretched                  |
| `--gold-500`       | `#c08a1f`   | HOA disclaimer accent (legacy only)   |

The same three families (`--ocean-*`, `--seafoam-*`, `--coral-*`) are used
in `index.html`, so when you customize one file the other stays in sync.

## Browser Support

- Chrome (latest)
- Firefox (latest)
- Safari (latest)
- Edge (latest)

## License

Free to use and modify for personal and commercial projects.
