var license = `/*
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
*/
`;

const gulp = require('gulp'),
  babel = require('gulp-babel'),
  concat = require('gulp-concat'),
  uglify = require('gulp-uglify'),
  ngAnnotate = require('gulp-ng-annotate'),
  rename = require('gulp-rename'),
  cleanCSS = require('gulp-clean-css'),
  del = require('del'),
  eslint = require('gulp-eslint'),
  maps = require('gulp-sourcemaps'),
  insert = require('gulp-insert'),

  tsc = require('gulp-typescript'),
  tslint = require('gulp-tslint');
  //tsProject = tsc.createProject('tsconfig.json');

  // temp directory for converted typescript files
const built_ts = 'built_ts';

// fetch command line arguments
const arg = (argList => {
  let arg = {}, a, opt, thisOpt, curOpt;
  for (a = 0; a < argList.length; a++) {
    thisOpt = argList[a].trim();
    opt = thisOpt.replace(/^-+/, '');

    if (opt === thisOpt) {
      // argument value
      if (curOpt) arg[curOpt] = opt;
      curOpt = null;
    }
    else {
      // argument name
      curOpt = opt;
      arg[curOpt] = true;
    }
  }
  return arg;
})(process.argv);

var src = arg.src ? arg.src + '/' : '';

const paths = {
  typescript: {
    src: src + 'plugin/**/*.ts',
    dest: built_ts
  },
  styles: {
    src: src + 'plugin/css/**/*.css',
    dest: 'dist/css/',
    vendor_files: ['node_modules/jquery-ui-dist/jquery-ui.css',
      'node_modules/patternfly/dist/css/patternfly.min.css',
      'node_modules/patternfly/dist/css/patternfly-additions.min.css',
      'node_modules/jquery.fancytree/dist/skin-bootstrap-n/ui.fancytree.css',
      'node_modules/c3/c3.css',
      'node_modules/angular-ui-grid/ui-grid.css'
    ]
  },
  scripts: {
    src: [src + 'plugin/js/**/*.js', built_ts + '/**/*.js'],
    dest: 'dist/js/',
    vendor_files: ['node_modules/bluebird/js/browser/bluebird.min.js', 
      'node_modules/jquery/dist/jquery.min.js',
      'node_modules/jquery-ui-dist/jquery-ui.min.js',
      'node_modules/jquery.fancytree/dist/jquery.fancytree-all.min.js',
      'node_modules/angular/angular.min.js',
      'node_modules/angular-animate/angular-animate.min.js',
      'node_modules/angular-sanitize/angular-sanitize.min.js',
      'node_modules/angular-route/angular-route.min.js',
      'node_modules/angular-resource/angular-resource.min.js',
      'node_modules/bootstrap/dist/js/bootstrap.min.js',
      'node_modules/angular-ui-bootstrap/dist/ui-bootstrap.js',
      'node_modules/angular-ui-bootstrap/dist/ui-bootstrap-tpls.js',
      'node_modules/d3/d3.min.js',
      'node_modules/d3-queue/build/d3-queue.min.js',
      'node_modules/d3-time/build/d3-time.min.js',
      'node_modules/d3-time-format/build/d3-time-format.min.js',
      'node_modules/d3-path/build/d3-path.min.js',
      'node_modules/c3/c3.min.js',
      'node_modules/angular-ui-slider/src/slider.js',
      'node_modules/angular-ui-grid/ui-grid.min.js',
      'node_modules/angular-bootstrap-checkbox/angular-bootstrap-checkbox.js',
      'node_modules/notifyjs-browser/dist/notify.js',
      'node_modules/patternfly/dist/js/patternfly.min.js',
      'node_modules/dispatch-management/dist/dispatch-management.min.js'
    ]
  }
};

function clean() {
  return del(['dist',built_ts ]);
}
function cleanup() {
  return del([built_ts]);
}
function styles() {
  return gulp.src(paths.styles.src)
    .pipe(maps.init())
    .pipe(cleanCSS())
    .pipe(rename({
      basename: 'dispatch',
      suffix: '.min'
    }))
    .pipe(insert.prepend(license))
    .pipe(maps.write('./'))
    .pipe(gulp.dest(paths.styles.dest));
}
function vendor_styles() {
  return gulp.src(paths.styles.vendor_files)
    .pipe(maps.init())
    .pipe(concat('vendor.css'))
    .pipe(cleanCSS())
    .pipe(rename({
      basename: 'vendor',
      suffix: '.min'
    }))
    .pipe(maps.write('./'))
    .pipe(gulp.dest(paths.styles.dest));
}

function scripts() {
  return gulp.src(paths.scripts.src, { sourcemaps: true })
    .pipe(babel({
      presets: [require.resolve('babel-preset-env')]
    }))
    .pipe(ngAnnotate())
    .pipe(maps.init())
    .pipe(uglify())
    .pipe(concat('dispatch.min.js'))
    .pipe(insert.prepend(license))
    .pipe(maps.write('./'))
    .pipe(gulp.dest(paths.scripts.dest));
}

function vendor_scripts() {
  return gulp.src(paths.scripts.vendor_files)
    .pipe(maps.init())
    .pipe(uglify())
    .pipe(concat('vendor.min.js'))
    .pipe(maps.write('./'))
    .pipe(gulp.dest(paths.scripts.dest));
}
function watch() {
  gulp.watch(paths.scripts.src, scripts);
  gulp.watch(paths.styles.src, styles);
}
function lint() {
  return gulp.src('plugin/**/*.js')
    .pipe(eslint())
    .pipe(eslint.format())
    .pipe(eslint.failAfterError());
}

function _typescript() {
  return tsProject.src({files: src + 'plugin/**/*.ts'})
    .pipe(tsProject())
    .js.pipe(gulp.dest('build/dist'));
}

function typescript() {
  var tsResult = gulp.src(paths.typescript.src)
    .pipe(tsc());
  return tsResult.js.pipe(gulp.dest(paths.typescript.dest));
}

function ts_lint() {
  return gulp.src('plugin/js/**/*.ts')
    .pipe(tslint({
      formatter: 'verbose'
    }))
    .pipe(tslint.report());
}

var build = gulp.series(
  clean,                          // removes the dist/ dir
  gulp.parallel(lint, ts_lint),   // lints the .js, .ts files
  typescript,                     // converts .ts to .js
  gulp.parallel(vendor_styles, vendor_scripts, styles, scripts) // uglify and concat
  //cleanup                         // remove .js that were converted from .ts
);

exports.clean = clean;
exports.watch = watch;
exports.build = build;
exports.lint = lint;
exports.tslint = ts_lint;
exports.tsc = typescript;
exports.scripts = scripts;
exports.styles = styles;

gulp.task('default', build);
