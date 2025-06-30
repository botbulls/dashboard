# Vendor JavaScript / CSS

This folder is intended to host minified third-party libraries that were previously loaded from public CDNs.

For production build copy the exact versions you need:

* jquery.min.js
* bootstrap.bundle.min.js
* chart.min.js + chartjs plugins
* moment.min.js
* daterangepicker.min.js / .css
* datatables.min.js / .css

During development the application will still work if these files are missing (scripts will 404 but are not executed in tests). Replace the files before deploying to a real server.