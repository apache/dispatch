/*
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
'use strict';

/* global angular d3 separateAddresses Traffic */
/**
 * @module QDR
 */
var QDR = (function(QDR) {

  /**
   * @method TopologyController
   *
   * Controller that handles the QDR topology page
   */

  QDR.module.controller('QDR.TopologyController', ['$scope', '$rootScope', 'QDRService', '$location', '$timeout', '$uibModal', '$sce',
    function($scope, $rootScope, QDRService, $location, $timeout, $uibModal, $sce) {

      const TOPOOPTIONSKEY = 'topoOptions';
      const radius = 25;
      const radiusNormal = 15;

      //  - nodes is an array of router/client info. these are the circles
      //  - links is an array of connections between the routers. these are the lines with arrows
      let nodes = [];
      let links = [];
      let forceData = {nodes: nodes, links: links};
      let urlPrefix = $location.absUrl();
      urlPrefix = urlPrefix.split('#')[0];
      QDR.log.debug('started QDR.TopologyController with urlPrefix: ' + urlPrefix);

      $scope.legendOptions = angular.fromJson(localStorage[TOPOOPTIONSKEY]) || {showTraffic: false, trafficType: 'dots'};
      if (!$scope.legendOptions.trafficType)
        $scope.legendOptions.trafficType = 'dots';
      $scope.legend = {status: {legendOpen: true, optionsOpen: true}};
      $scope.legend.status.optionsOpen = $scope.legendOptions.showTraffic;
      let traffic = new Traffic($scope, $timeout, QDRService, separateAddresses, 
        radius, forceData, nextHop, $scope.legendOptions.trafficType, urlPrefix);

      // the showTraaffic checkbox was just toggled (or initialized)
      $scope.$watch('legend.status.optionsOpen', function () {
        $scope.legendOptions.showTraffic = $scope.legend.status.optionsOpen;
        localStorage[TOPOOPTIONSKEY] = JSON.stringify($scope.legendOptions);
        if ($scope.legend.status.optionsOpen) {
          traffic.start();
        } else {
          traffic.stop();
          traffic.remove();
          restart();
        }
      });
      $scope.$watch('legendOptions.trafficType', function () {
        localStorage[TOPOOPTIONSKEY] = JSON.stringify($scope.legendOptions);
        if ($scope.legendOptions.showTraffic) {
          restart();
          traffic.setAnimationType($scope.legendOptions.trafficType, separateAddresses, radius);
          traffic.start();
        }
      });

      // mouse event vars
      let selected_node = null,
        selected_link = null,
        mousedown_link = null,
        mousedown_node = null,
        mouseover_node = null,
        mouseup_node = null,
        initial_mouse_down_position = null;

      $scope.schema = 'Not connected';

      $scope.contextNode = null; // node that is associated with the current context menu
      $scope.isRight = function(mode) {
        return mode.right;
      };

      var setNodesFixed = function (name, b) {
        nodes.some(function (n) {
          if (n.name === name) {
            n.fixed = b;
            return true;
          }
        });
      };
      $scope.setFixed = function(b) {
        if ($scope.contextNode) {
          $scope.contextNode.fixed = b;
          setNodesFixed($scope.contextNode.name, b);
          savePositions();
        }
        restart();
      };
      $scope.isFixed = function() {
        if (!$scope.contextNode)
          return false;
        return ($scope.contextNode.fixed & 1);
      };

      let mouseX, mouseY;
      var relativeMouse = function () {
        let offset = $('#main_container').offset();
        return {left: (mouseX + $(document).scrollLeft()) - 1,
          top: (mouseY  + $(document).scrollTop()) - 1,
          offset: offset
        };
      };
      // event handlers for popup context menu
      $(document).mousemove(function(e) {
        mouseX = e.clientX;
        mouseY = e.clientY;
      });
      $(document).mousemove();
      $(document).click(function() {
        $scope.contextNode = null;
        $('.contextMenu').fadeOut(200);
      });

      const radii = {
        'inter-router': 25,
        'normal': 15,
        'on-demand': 15,
        'route-container': 15,
      };
      let svg, lsvg;
      let force;
      let animate = false; // should the force graph organize itself when it is displayed
      let path, circle;
      let savedKeys = {};
      let width = 0;
      let height = 0;

      var getSizes = function() {
        const gap = 5;
        let legendWidth = 194;
        let topoWidth = $('#topology').width();
        if (topoWidth < 768)
          legendWidth = 0;
        let width = $('#topology').width() - gap - legendWidth;
        let top = $('#topology').offset().top;
        let height = window.innerHeight - top - gap;
        if (width < 10) {
          QDR.log.info('page width and height are abnormal w:' + width + ' height:' + height);
          return [0, 0];
        }
        return [width, height];
      };
      var resize = function() {
        if (!svg)
          return;
        let sizes = getSizes();
        width = sizes[0];
        height = sizes[1];
        if (width > 0) {
          // set attrs and 'resume' force
          svg.attr('width', width);
          svg.attr('height', height);
          force.size(sizes).resume();
        }
        $timeout(createLegend);
      };

      // the window is narrow and the page menu icon was clicked.
      // Re-create the legend
      $scope.$on('pageMenuClicked', function () {
        $timeout(createLegend);
      });

      window.addEventListener('resize', resize);
      let sizes = getSizes();
      width = sizes[0];
      height = sizes[1];
      if (width <= 0 || height <= 0)
        return;

      var nodeExists = function (connectionContainer) {
        return nodes.findIndex( function (node) {
          return node.container === connectionContainer;
        });
      };
      var normalExists = function (connectionContainer) {
        let normalInfo = {};
        for (let i=0; i<nodes.length; ++i) {
          if (nodes[i].normals) {
            if (nodes[i].normals.some(function (normal, j) {
              if (normal.container === connectionContainer && i !== j) {
                normalInfo = {nodesIndex: i, normalsIndex: j};
                return true;
              }
              return false;
            }))
              break;
          }
        }
        return normalInfo;
      };
      var getLinkSource = function (nodesIndex) {
        for (let i=0; i<links.length; ++i) {
          if (links[i].target === nodesIndex)
            return i;
        }
        return -1;
      };
      var aNode = function(id, name, nodeType, nodeInfo, nodeIndex, x, y, connectionContainer, resultIndex, fixed, properties) {
        properties = properties || {};
        for (let i=0; i<nodes.length; ++i) {
          if (nodes[i].name === name || nodes[i].container === connectionContainer) {
            if (properties.product)
              nodes[i].properties = properties;
            return nodes[i];
          }
        }
        let routerId = QDRService.management.topology.nameFromId(id);
        return {
          key: id,
          name: name,
          nodeType: nodeType,
          properties: properties,
          routerId: routerId,
          x: x,
          y: y,
          id: nodeIndex,
          resultIndex: resultIndex,
          fixed: !!+fixed,
          cls: '',
          container: connectionContainer
        };
      };

      var getLinkDir = function (id, connection, onode) {
        let links = onode['router.link'];
        if (!links) {
          return 'unknown';
        }
        let inCount = 0, outCount = 0;
        links.results.forEach( function (linkResult) {
          let link = QDRService.utilities.flatten(links.attributeNames, linkResult);
          if (link.linkType === 'endpoint' && link.connectionId === connection.identity)
            if (link.linkDir === 'in')
              ++inCount;
            else
              ++outCount;
        });
        if (inCount > 0 && outCount > 0)
          return 'both';
        if (inCount > 0)
          return 'in';
        if (outCount > 0)
          return 'out';
        return 'unknown';
      };

      var savePositions = function () {
        nodes.forEach( function (d) {
          localStorage[d.name] = angular.toJson({
            x: Math.round(d.x),
            y: Math.round(d.y),
            fixed: (d.fixed & 1) ? 1 : 0,
          });
        });
      };

      var initializeNodes = function (nodeInfo) {
        let nodeCount = Object.keys(nodeInfo).length;
        let yInit = 50;
        forceData.nodes = nodes = [];
        for (let id in nodeInfo) {
          let name = QDRService.management.topology.nameFromId(id);
          // if we have any new nodes, animate the force graph to position them
          let position = angular.fromJson(localStorage[name]);
          if (!angular.isDefined(position)) {
            animate = true;
            position = {
              x: Math.round(width / 4 + ((width / 2) / nodeCount) * nodes.length),
              y: Math.round(height / 2 + Math.sin(nodes.length / (Math.PI*2.0)) * height / 4),
              fixed: false,
            };
            //QDR.log.debug("new node pos (" + position.x + ", " + position.y + ")")
          }
          if (position.y > height) {
            position.y = 200 - yInit;
            yInit *= -1;
          }
          nodes.push(aNode(id, name, 'inter-router', nodeInfo, nodes.length, position.x, position.y, name, undefined, position.fixed));
        }
      };

      var initializeLinks = function (nodeInfo, unknowns) {
        forceData.links = links = [];
        let source = 0;
        let client = 1.0;
        for (let id in nodeInfo) {
          let onode = nodeInfo[id];
          if (!onode['connection'])
            continue;
          let conns = onode['connection'].results;
          let attrs = onode['connection'].attributeNames;
          //QDR.log.debug("external client parent is " + parent);
          let normalsParent = {}; // 1st normal node for this parent

          for (let j = 0; j < conns.length; j++) {
            let connection = QDRService.utilities.flatten(attrs, conns[j]);
            let role = connection.role;
            let properties = connection.properties || {};
            let dir = connection.dir;
            if (role == 'inter-router') {
              let connId = connection.container;
              let target = getContainerIndex(connId, nodeInfo);
              if (target >= 0) {
                getLink(source, target, dir, '', source + '-' + target);
              }
            } /* else if (role == "normal" || role == "on-demand" || role === "route-container")*/ {
              // not an connection between routers, but an external connection
              let name = QDRService.management.topology.nameFromId(id) + '.' + connection.identity;

              // if we have any new clients, animate the force graph to position them
              let position = angular.fromJson(localStorage[name]);
              if (!angular.isDefined(position)) {
                animate = true;
                position = {
                  x: Math.round(nodes[source].x + 40 * Math.sin(client / (Math.PI * 2.0))),
                  y: Math.round(nodes[source].y + 40 * Math.cos(client / (Math.PI * 2.0))),
                  fixed: false
                };
                //QDR.log.debug("new client pos (" + position.x + ", " + position.y + ")")
              }// else QDR.log.debug("using previous location")
              if (position.y > height) {
                position.y = Math.round(nodes[source].y + 40 + Math.cos(client / (Math.PI * 2.0)));
              }
              let existingNodeIndex = nodeExists(connection.container);
              let normalInfo = normalExists(connection.container);
              let node = aNode(id, name, role, nodeInfo, nodes.length, position.x, position.y, connection.container, j, position.fixed, properties);
              let nodeType = QDRService.utilities.isAConsole(properties, connection.identity, role, node.key) ? 'console' : 'client';
              let cdir = getLinkDir(id, connection, onode);
              if (existingNodeIndex >= 0) {
                // make a link between the current router (source) and the existing node
                getLink(source, existingNodeIndex, dir, 'small', connection.name);
              } else if (normalInfo.nodesIndex) {
                // get node index of node that contained this connection in its normals array
                let normalSource = getLinkSource(normalInfo.nodesIndex);
                if (normalSource >= 0) {
                  if (cdir === 'unknown')
                    cdir = dir;
                  node.cdir = cdir;
                  nodes.push(node);
                  // create link from original node to the new node
                  getLink(links[normalSource].source, nodes.length-1, cdir, 'small', connection.name);
                  // create link from this router to the new node
                  getLink(source, nodes.length-1, cdir, 'small', connection.name);
                  // remove the old node from the normals list
                  nodes[normalInfo.nodesIndex].normals.splice(normalInfo.normalsIndex, 1);
                }
              } else if (role === 'normal') {
              // normal nodes can be collapsed into a single node if they are all the same dir
                if (cdir !== 'unknown') {
                  node.user = connection.user;
                  node.isEncrypted = connection.isEncrypted;
                  node.host = connection.host;
                  node.connectionId = connection.identity;
                  node.cdir = cdir;
                  // determine arrow direction by using the link directions
                  if (!normalsParent[nodeType+cdir]) {
                    normalsParent[nodeType+cdir] = node;
                    nodes.push(node);
                    node.normals = [node];
                    // now add a link
                    getLink(source, nodes.length - 1, cdir, 'small', connection.name);
                    client++;
                  } else {
                    normalsParent[nodeType+cdir].normals.push(node);
                  }
                } else {
                  node.id = nodes.length - 1 + unknowns.length;
                  unknowns.push(node);
                }
              } else {
                nodes.push(node);
                // now add a link
                getLink(source, nodes.length - 1, dir, 'small', connection.name);
                client++;
              }
            }
          }
          source++;
        }
      };

      // vary the following force graph attributes based on nodeCount
      // <= 6 routers returns min, >= 80 routers returns max, interpolate linearly
      var forceScale = function(nodeCount, min, max) {
        let count = nodeCount;
        if (nodeCount < 6) count = 6;
        if (nodeCount > 80) count = 80;
        let x = d3.scale.linear()
          .domain([6,80])
          .range([min, max]);
        //QDR.log.debug("forceScale(" + nodeCount + ", " + min + ", " + max + "  returns " + x(count) + " " + x(nodeCount))
        return x(count);
      };
      var linkDistance = function (d, nodeCount) {
        if (d.target.nodeType === 'inter-router')
          return forceScale(nodeCount, 150, 70);
        return forceScale(nodeCount, 75, 40);
      };
      var charge = function (d, nodeCount) {
        if (d.nodeType === 'inter-router')
          return forceScale(nodeCount, -1800, -900);
        return -900;
      };
      var gravity = function (d, nodeCount) {
        return forceScale(nodeCount, 0.0001, 0.1);
      };
      // initialize the nodes and links array from the QDRService.topology._nodeInfo object
      var initForceGraph = function() {
        forceData.nodes = nodes = [];
        forceData.links = links = [];
        let nodeInfo = QDRService.management.topology.nodeInfo();
        let nodeCount = Object.keys(nodeInfo).length;

        let oldSelectedNode = selected_node;
        let oldMouseoverNode = mouseover_node;
        mouseover_node = null;
        selected_node = null;
        selected_link = null;

        savePositions();
        d3.select('#SVG_ID').remove();
        svg = d3.select('#topology')
          .append('svg')
          .attr('id', 'SVG_ID')
          .attr('width', width)
          .attr('height', height);

        // the legend
        d3.select('#topo_svg_legend svg').remove();
        lsvg = d3.select('#topo_svg_legend')
          .append('svg')
          .attr('id', 'svglegend');
        lsvg = lsvg.append('svg:g')
          .attr('transform', 'translate(' + (radii['inter-router'] + 2) + ',' + (radii['inter-router'] + 2) + ')')
          .selectAll('g');

        // mouse event vars
        mousedown_link = null;
        mousedown_node = null;
        mouseup_node = null;

        // initialize the list of nodes
        initializeNodes(nodeInfo);
        savePositions();

        // initialize the list of links
        let unknowns = [];
        initializeLinks(nodeInfo, unknowns);
        $scope.schema = QDRService.management.schema();
        // init D3 force layout
        force = d3.layout.force()
          .nodes(nodes)
          .links(links)
          .size([width, height])
          .linkDistance(function(d) { return linkDistance(d, nodeCount); })
          .charge(function(d) { return charge(d, nodeCount); })
          .friction(.10)
          .gravity(function(d) { return gravity(d, nodeCount); })
          .on('tick', tick)
          .on('end', function () {savePositions();})
          .start();

        // This section adds in the arrows
        svg.append('svg:defs').attr('class', 'marker-defs').selectAll('marker')
          .data(['end-arrow', 'end-arrow-selected', 'end-arrow-small', 'end-arrow-highlighted', 
            'start-arrow', 'start-arrow-selected', 'start-arrow-small', 'start-arrow-highlighted'])
          .enter().append('svg:marker') 
          .attr('id', function (d) { return d; })
          .attr('viewBox', '0 -5 10 10')
          .attr('refX', function (d) { 
            if (d.substr(0, 3) === 'end') {
              return 24;
            }
            return d !== 'start-arrow-small' ? -14 : -24;})
          .attr('markerWidth', 4)
          .attr('markerHeight', 4)
          .attr('orient', 'auto')
          .classed('small', function (d) {return d.indexOf('small') > -1;})
          .append('svg:path')
          .attr('d', function (d) {
            return d.substr(0, 3) === 'end' ? 'M 0 -5 L 10 0 L 0 5 z' : 'M 10 -5 L 0 0 L 10 5 z';
          });

        // gradient for sender/receiver client
        let grad = svg.append('svg:defs').append('linearGradient')
          .attr('id', 'half-circle')
          .attr('x1', '0%')
          .attr('x2', '0%')
          .attr('y1', '100%')
          .attr('y2', '0%');
        grad.append('stop').attr('offset', '50%').style('stop-color', '#C0F0C0');
        grad.append('stop').attr('offset', '50%').style('stop-color', '#F0F000');

        // handles to link and node element groups
        path = svg.append('svg:g').selectAll('path'),
        circle = svg.append('svg:g').selectAll('g');

        // app starts here
        restart(false);
        force.start();
        if (oldSelectedNode) {
          d3.selectAll('circle.inter-router').classed('selected', function (d) {
            if (d.key === oldSelectedNode.key) {
              selected_node = d;
              return true;
            }
            return false;
          });
        }
        if (oldMouseoverNode && selected_node) {
          d3.selectAll('circle.inter-router').each(function (d) {
            if (d.key === oldMouseoverNode.key) {
              mouseover_node = d;
              QDRService.management.topology.ensureAllEntities([{entity: 'router.node', attrs: ['id','nextHop']}], function () {
                nextHop(selected_node, d);
                restart();
              });
            }
          });
        }

        // if any clients don't yet have link directions, get the links for those nodes and restart the graph
        if (unknowns.length > 0)
          setTimeout(resolveUnknowns, 10, nodeInfo, unknowns);

        var continueForce = function (extra) {
          if (extra > 0) {
            --extra;
            force.start();
            setTimeout(continueForce, 100, extra);
          }
        };
        continueForce(forceScale(nodeCount, 0, 200));  // give graph time to settle down
      };

      var resolveUnknowns = function (nodeInfo, unknowns) {
        let unknownNodes = {};
        // collapse the unknown node.keys using an object
        for (let i=0; i<unknowns.length; ++i) {
          unknownNodes[unknowns[i].key] = 1;
        }
        unknownNodes = Object.keys(unknownNodes);
        //QDR.log.info("-- resolveUnknowns: ensuring .connection and .router.link are present for each node")
        QDRService.management.topology.ensureEntities(unknownNodes, [{entity: 'connection', force: true}, 
          {entity: 'router.link', attrs: ['linkType','connectionId','linkDir'], force: true}], function () {
          nodeInfo = QDRService.management.topology.nodeInfo();
          initializeLinks(nodeInfo, []);
          // collapse any router-container nodes that are duplicates
          animate = true;
          force.nodes(nodes).links(links).start();
          restart(false);
        });
      };

      function getContainerIndex(_id, nodeInfo) {
        let nodeIndex = 0;
        for (let id in nodeInfo) {
          if (QDRService.management.topology.nameFromId(id) === _id)
            return nodeIndex;
          ++nodeIndex;
        }
        return -1;
      }

      function getLink(_source, _target, dir, cls, uid) {
        for (let i = 0; i < links.length; i++) {
          let s = links[i].source,
            t = links[i].target;
          if (typeof links[i].source == 'object') {
            s = s.id;
            t = t.id;
          }
          if (s == _source && t == _target) {
            return i;
          }
          // same link, just reversed
          if (s == _target && t == _source) {
            return -i;
          }
        }
        //QDR.log.debug("creating new link (" + (links.length) + ") between " + nodes[_source].name + " and " + nodes[_target].name);
        if (links.some( function (l) { return l.uid === uid;}))
          uid = uid + '.' + links.length;
        let link = {
          source: _source,
          target: _target,
          left: dir != 'out',
          right: (dir == 'out' || dir == 'both'),
          cls: cls,
          uid: uid,
        };
        return links.push(link) - 1;
      }


      function resetMouseVars() {
        mousedown_node = null;
        mouseover_node = null;
        mouseup_node = null;
        mousedown_link = null;
      }

      // update force layout (called automatically each iteration)
      function tick() {
        circle.attr('transform', function(d) {
          let cradius;
          if (d.nodeType == 'inter-router') {
            cradius = d.left ? radius + 8 : radius;
          } else {
            cradius = d.left ? radiusNormal + 18 : radiusNormal;
          }
          d.x = Math.max(d.x, radiusNormal * 2);
          d.y = Math.max(d.y, radiusNormal * 2);
          d.x = Math.max(0, Math.min(width - cradius, d.x));
          d.y = Math.max(0, Math.min(height - cradius, d.y));
          return 'translate(' + d.x + ',' + d.y + ')';
        });

        // draw directed edges with proper padding from node centers
        path.attr('d', function(d) {
          let sourcePadding, targetPadding, r;

          r = d.target.nodeType === 'inter-router' ? radius : radiusNormal - 18;
          sourcePadding = targetPadding = 0;
          let dtx = Math.max(targetPadding, Math.min(width - r, d.target.x)),
            dty = Math.max(targetPadding, Math.min(height - r, d.target.y)),
            dsx = Math.max(sourcePadding, Math.min(width - r, d.source.x)),
            dsy = Math.max(sourcePadding, Math.min(height - r, d.source.y));

          let deltaX = dtx - dsx,
            deltaY = dty - dsy,
            dist = Math.sqrt(deltaX * deltaX + deltaY * deltaY);
          if (dist == 0)
            dist = 0.001;
          let normX = deltaX / dist,
            normY = deltaY / dist;
          let sourceX = dsx + (sourcePadding * normX),
            sourceY = dsy + (sourcePadding * normY),
            targetX = dtx - (targetPadding * normX),
            targetY = dty - (targetPadding * normY);
          sourceX = Math.max(0, sourceX);
          sourceY = Math.max(0, sourceY);
          targetX = Math.max(0, targetX);
          targetY = Math.max(0, targetY);

          return 'M' + sourceX + ',' + sourceY + 'L' + targetX + ',' + targetY;
        })
          .attr('id', function (d) {
            return ['path', d.source.index, d.target.index].join('-');
          });

        if (!animate) {
          animate = true;
          force.stop();
        }
      }

      // highlight the paths between the selected node and the hovered node
      function findNextHopNode(from, d) {
        // d is the node that the mouse is over
        // from is the selected_node ....
        if (!from)
          return null;

        if (from == d)
          return selected_node;

        //QDR.log.debug("finding nextHop from: " + from.name + " to " + d.name);
        let sInfo = QDRService.management.topology.nodeInfo()[from.key];

        if (!sInfo) {
          QDR.log.warn('unable to find topology node info for ' + from.key);
          return null;
        }

        // find the hovered name in the selected name's .router.node results
        if (!sInfo['router.node'])
          return null;
        let aAr = sInfo['router.node'].attributeNames;
        let vAr = sInfo['router.node'].results;
        for (let hIdx = 0; hIdx < vAr.length; ++hIdx) {
          let addrT = QDRService.utilities.valFor(aAr, vAr[hIdx], 'id');
          if (addrT == d.name) {
            //QDR.log.debug("found " + d.name + " at " + hIdx);
            let nextHop = QDRService.utilities.valFor(aAr, vAr[hIdx], 'nextHop');
            //QDR.log.debug("nextHop was " + nextHop);
            return (nextHop == null) ? nodeFor(addrT) : nodeFor(nextHop);
          }
        }
        return null;
      }

      function nodeFor(name) {
        for (let i = 0; i < nodes.length; ++i) {
          if (nodes[i].name == name)
            return nodes[i];
        }
        return null;
      }

      function linkFor(source, target) {
        for (let i = 0; i < links.length; ++i) {
          if ((links[i].source == source) && (links[i].target == target))
            return links[i];
          if ((links[i].source == target) && (links[i].target == source))
            return links[i];
        }
        // the selected node was a client/broker
        return null;
      }

      function clearPopups() {
        d3.select('#crosssection').style('display', 'none');
        $('.hastip').empty();
        d3.select('#multiple_details').style('display', 'none');
        d3.select('#link_details').style('display', 'none');
        d3.select('#node_context_menu').style('display', 'none');

      }

      function clerAllHighlights() {
        for (let i = 0; i < links.length; ++i) {
          links[i]['highlighted'] = false;
        }
        for (let i = 0; i<nodes.length; ++i) {
          nodes[i]['highlighted'] = false;
        }
      }
      // takes the nodes and links array of objects and adds svg elements for everything that hasn't already
      // been added
      function restart(start) {
        if (!circle)
          return;
        circle.call(force.drag);

        // path (link) group
        path = path.data(links, function(d) {return d.uid;});

        // update existing links
        path.classed('selected', function(d) {
          return d === selected_link;
        })
          .classed('highlighted', function(d) {
            return d.highlighted;
          });
        if (!$scope.legend.status.optionsOpen || $scope.legendOptions.trafficType === 'dots') {
          path
            .attr('marker-start', function(d) {
              let sel = d === selected_link ? '-selected' : (d.cls === 'small' ? '-small' : '');
              if (d.highlighted)
                sel = '-highlighted';
              return d.left ? 'url(' + urlPrefix + '#start-arrow' + sel + ')' : '';
            })
            .attr('marker-end', function(d) {
              let sel = d === selected_link ? '-selected' : (d.cls === 'small' ? '-small' : '');
              if (d.highlighted)
                sel = '-highlighted';
              return d.right ? 'url(' + urlPrefix + '#end-arrow' + sel + ')' : '';
            });
        }
        // add new links. if a link with a new uid is found in the data, add a new path
        path.enter().append('svg:path')
          .attr('class', 'link')
          .attr('marker-start', function(d) {
            let sel = d === selected_link ? '-selected' : (d.cls === 'small' ? '-small' : '');
            return d.left ? 'url(' + urlPrefix + '#start-arrow' + sel + ')' : '';
          })
          .attr('marker-end', function(d) {
            let sel = d === selected_link ? '-selected' : (d.cls === 'small' ? '-small' : '');
            return d.right ? 'url(' + urlPrefix + '#end-arrow' + sel + ')' : '';
          })
          .classed('small', function(d) {
            return d.cls == 'small';
          })
          .on('mouseover', function(d) { // mouse over a path
            let event = d3.event;
            mousedown_link = d;
            selected_link = mousedown_link;
            let updateTooltip = function () {
              $timeout(function () {
                $scope.trustedpopoverContent = $sce.trustAsHtml(connectionPopupHTML(d));
                if (selected_link)
                  displayTooltip(event);
              });
            };
            // update the contents of the popup tooltip each time the data is polled
            QDRService.management.topology.addUpdatedAction('connectionPopupHTML', updateTooltip);
            QDRService.management.topology.ensureAllEntities(
              [{ entity: 'router.link', force: true},{entity: 'connection'}], function () {
                updateTooltip();
              });
            // show the tooltip
            updateTooltip();
            restart();

          })
          .on('mouseout', function() { // mouse out of a path
            QDRService.management.topology.delUpdatedAction('connectionPopupHTML');
            d3.select('#popover-div')
              .style('display', 'none');
            selected_link = null;
            restart();
          })
          // left click a path
          .on('click', function () {
            d3.event.stopPropagation();
            clearPopups();
          });
        // remove old links
        path.exit().remove();


        // circle (node) group
        // nodes are known by id
        circle = circle.data(nodes, function(d) {
          return d.name;
        });

        // update existing nodes visual states
        circle.selectAll('circle')
          .classed('highlighted', function(d) {
            return d.highlighted;
          })
          .classed('selected', function(d) {
            return (d === selected_node);
          })
          .classed('fixed', function(d) {
            return d.fixed & 1;
          });

        // add new circle nodes
        let g = circle.enter().append('svg:g')
          .classed('multiple', function(d) {
            return (d.normals && d.normals.length > 1);
          })
          .attr('id', function (d) { return (d.nodeType !== 'normal' ? 'router' : 'client') + '-' + d.index; });

        appendCircle(g)
          .on('mouseover', function(d) {  // mouseover a circle
            QDRService.management.topology.delUpdatedAction('connectionPopupHTML');
            if (d.nodeType === 'normal') {
              showClientTooltip(d, d3.event);
            } else
              showRouterTooltip(d, d3.event);
            if (d === mousedown_node)
              return;
            // enlarge target node
            d3.select(this).attr('transform', 'scale(1.1)');
            if (!selected_node) {
              return;
            }
            // highlight the next-hop route from the selected node to this node
            clerAllHighlights();
            // we need .router.node info to highlight hops
            QDRService.management.topology.ensureAllEntities([{entity: 'router.node', attrs: ['id','nextHop']}], function () {
              mouseover_node = d;  // save this node in case the topology changes so we can restore the highlights
              nextHop(selected_node, d);
              restart();
            });
          })
          .on('mouseout', function() { // mouse out for a circle
            // unenlarge target node
            d3.select('#popover-div')
              .style('display', 'none');
            d3.select(this).attr('transform', '');
            clerAllHighlights();
            mouseover_node = null;
            restart();
          })
          .on('mousedown', function(d) { // mouse down for circle
            if (d3.event.button !== 0) { // ignore all but left button
              return;
            }
            mousedown_node = d;
            // mouse position relative to svg
            initial_mouse_down_position = d3.mouse(this.parentNode.parentNode.parentNode).slice();
          })
          .on('mouseup', function(d) {  // mouse up for circle
            if (!mousedown_node)
              return;

            selected_link = null;
            // unenlarge target node
            d3.select(this).attr('transform', '');

            // check for drag
            mouseup_node = d;

            let mySvg = this.parentNode.parentNode.parentNode;
            // if we dragged the node, make it fixed
            let cur_mouse = d3.mouse(mySvg);
            if (cur_mouse[0] != initial_mouse_down_position[0] ||
              cur_mouse[1] != initial_mouse_down_position[1]) {
              d.fixed = true;
              setNodesFixed(d.name, true);
              resetMouseVars();
              restart();
              return;
            }

            // if this node was selected, unselect it
            if (mousedown_node === selected_node) {
              selected_node = null;
            } else {
              if (d.nodeType !== 'normal' && d.nodeType !== 'on-demand')
                selected_node = mousedown_node;
            }
            clerAllHighlights();
            mousedown_node = null;
            if (!$scope.$$phase) $scope.$apply();
            restart(false);

          })
          .on('dblclick', function(d) { // circle
            if (d.fixed) {
              d.fixed = false;
              setNodesFixed(d.name, false);
              restart(); // redraw the node without a dashed line
              force.start(); // let the nodes move to a new position
            }
          })
          .on('contextmenu', function(d) {  // circle
            $(document).click();
            d3.event.preventDefault();
            let rm = relativeMouse();
            d3.select('#node_context_menu')
              .style({
                display: 'block',
                left: rm.left + 'px',
                top: (rm.top - rm.offset.top) + 'px'
              });
            $timeout( function () {
              $scope.contextNode = d;
            });
          })
          .on('click', function(d) {  // circle
            if (!mouseup_node)
              return;
            // clicked on a circle
            clearPopups();
            if (!d.normals) {
              // circle was a router or a broker
              if (QDRService.utilities.isArtemis(d)) {
                const artemisPath = '/jmx/attributes?tab=artemis&con=Artemis';
                if (QDR.isStandalone)
                  window.location = $location.protocol() + '://localhost:8161/hawtio' + artemisPath;
                else
                  $location.path(artemisPath);
              }
              return;
            }
            d3.event.stopPropagation();
          });

        appendContent(g);
        //appendTitle(g);

        // remove old nodes
        circle.exit().remove();

        // add text to client circles if there are any that represent multiple clients
        svg.selectAll('.subtext').remove();
        let multiples = svg.selectAll('.multiple');
        multiples.each(function(d) {
          let g = d3.select(this);
          g.append('svg:text')
            .attr('x', radiusNormal + 3)
            .attr('y', Math.floor(radiusNormal / 2))
            .attr('class', 'subtext')
            .text('x ' + d.normals.length);
        });
        // call createLegend in timeout because:
        // If we create the legend right away, then it will be destroyed when the accordian
        // gets initialized as the page loads.
        $timeout(createLegend);

        if (!mousedown_node || !selected_node)
          return;

        if (!start)
          return;
        // set the graph in motion
        //QDR.log.debug("mousedown_node is " + mousedown_node);
        force.start();

      }
      let createLegend = function () {
        // dynamically create the legend based on which node types are present
        // the legend
        d3.select('#topo_svg_legend svg').remove();
        lsvg = d3.select('#topo_svg_legend')
          .append('svg')
          .attr('id', 'svglegend');
        lsvg = lsvg.append('svg:g')
          .attr('transform', 'translate(' + (radii['inter-router'] + 2) + ',' + (radii['inter-router'] + 2) + ')')
          .selectAll('g');
        let legendNodes = [];
        legendNodes.push(aNode('Router', '', 'inter-router', '', undefined, 0, 0, 0, 0, false, {}));

        if (!svg.selectAll('circle.console').empty()) {
          legendNodes.push(aNode('Console', '', 'normal', '', undefined, 1, 0, 0, 0, false, {
            console_identifier: 'Dispatch console'
          }));
        }
        if (!svg.selectAll('circle.client.in').empty()) {
          let node = aNode('Sender', '', 'normal', '', undefined, 2, 0, 0, 0, false, {});
          node.cdir = 'in';
          legendNodes.push(node);
        }
        if (!svg.selectAll('circle.client.out').empty()) {
          let node = aNode('Receiver', '', 'normal', '', undefined, 3, 0, 0, 0, false, {});
          node.cdir = 'out';
          legendNodes.push(node);
        }
        if (!svg.selectAll('circle.client.inout').empty()) {
          let node = aNode('Sender/Receiver', '', 'normal', '', undefined, 4, 0, 0, 0, false, {});
          node.cdir = 'both';
          legendNodes.push(node);
        }
        if (!svg.selectAll('circle.qpid-cpp').empty()) {
          legendNodes.push(aNode('Qpid broker', '', 'route-container', '', undefined, 5, 0, 0, 0, false, {
            product: 'qpid-cpp'
          }));
        }
        if (!svg.selectAll('circle.artemis').empty()) {
          legendNodes.push(aNode('Artemis broker', '', 'route-container', '', undefined, 6, 0, 0, 0, false,
            {product: 'apache-activemq-artemis'}));
        }
        if (!svg.selectAll('circle.route-container').empty()) {
          legendNodes.push(aNode('Service', '', 'route-container', 'external-service', undefined, 7, 0, 0, 0, false,
            {product: ' External Service'}));
        }
        lsvg = lsvg.data(legendNodes, function(d) {
          return d.key;
        });
        let lg = lsvg.enter().append('svg:g')
          .attr('transform', function(d, i) {
            // 45px between lines and add 10px space after 1st line
            return 'translate(0, ' + (45 * i + (i > 0 ? 10 : 0)) + ')';
          });

        appendCircle(lg);
        appendContent(lg);
        appendTitle(lg);
        lg.append('svg:text')
          .attr('x', 35)
          .attr('y', 6)
          .attr('class', 'label')
          .text(function(d) {
            return d.key;
          });
        lsvg.exit().remove();
        let svgEl = document.getElementById('svglegend');
        if (svgEl) {
          let bb;
          // firefox can throw an exception on getBBox on an svg element
          try {
            bb = svgEl.getBBox();
          } catch (e) {
            bb = {
              y: 0,
              height: 200,
              x: 0,
              width: 200
            };
          }
          svgEl.style.height = (bb.y + bb.height) + 'px';
          svgEl.style.width = (bb.x + bb.width) + 'px';
        }
      };
      let appendCircle = function(g) {
        // add new circles and set their attr/class/behavior
        return g.append('svg:circle')
          .attr('class', 'node')
          .attr('r', function(d) {
            return radii[d.nodeType];
          })
          .attr('fill', function (d) {
            if (d.cdir === 'both' && !QDRService.utilities.isConsole(d)) {
              return 'url(' + urlPrefix + '#half-circle)';
            }
            return null;
          })
          .classed('fixed', function(d) {
            return d.fixed & 1;
          })
          .classed('normal', function(d) {
            return d.nodeType == 'normal' || QDRService.utilities.isConsole(d);
          })
          .classed('in', function(d) {
            return d.cdir == 'in';
          })
          .classed('out', function(d) {
            return d.cdir == 'out';
          })
          .classed('inout', function(d) {
            return d.cdir == 'both';
          })
          .classed('inter-router', function(d) {
            return d.nodeType == 'inter-router';
          })
          .classed('on-demand', function(d) {
            return d.nodeType == 'on-demand';
          })
          .classed('console', function(d) {
            return QDRService.utilities.isConsole(d);
          })
          .classed('artemis', function(d) {
            return QDRService.utilities.isArtemis(d);
          })
          .classed('qpid-cpp', function(d) {
            return QDRService.utilities.isQpid(d);
          })
          .classed('route-container', function (d) {
            return (!QDRService.utilities.isArtemis(d) && !QDRService.utilities.isQpid(d) && d.nodeType === 'route-container');
          })
          .classed('client', function(d) {
            return d.nodeType === 'normal' && !d.properties.console_identifier;
          });
      };
      let appendContent = function(g) {
        // show node IDs
        g.append('svg:text')
          .attr('x', 0)
          .attr('y', function(d) {
            let y = 7;
            if (QDRService.utilities.isArtemis(d))
              y = 8;
            else if (QDRService.utilities.isQpid(d))
              y = 9;
            else if (d.nodeType === 'inter-router')
              y = 4;
            else if (d.nodeType === 'route-container')
              y = 5;
            return y;
          })
          .attr('class', 'id')
          .classed('console', function(d) {
            return QDRService.utilities.isConsole(d);
          })
          .classed('normal', function(d) {
            return d.nodeType === 'normal';
          })
          .classed('on-demand', function(d) {
            return d.nodeType === 'on-demand';
          })
          .classed('artemis', function(d) {
            return QDRService.utilities.isArtemis(d);
          })
          .classed('qpid-cpp', function(d) {
            return QDRService.utilities.isQpid(d);
          })
          .text(function(d) {
            if (QDRService.utilities.isConsole(d)) {
              return '\uf108'; // icon-desktop for this console
            } else if (QDRService.utilities.isArtemis(d)) {
              return '\ue900';
            } else if (QDRService.utilities.isQpid(d)) {
              return '\ue901';
            } else if (d.nodeType === 'route-container') {
              return d.properties.product ? d.properties.product[0].toUpperCase() : 'S';
            } else if (d.nodeType === 'normal')
              return '\uf109'; // icon-laptop for clients
            return d.name.length > 7 ? d.name.substr(0, 6) + '...' : d.name;
          });
      };
      let appendTitle = function(g) {
        g.append('svg:title').text(function(d) {
          return generateTitle(d);
        });
      };

      let generateTitle = function (d) {
        let x = '';
        if (d.normals && d.normals.length > 1)
          x = ' x ' + d.normals.length;
        if (QDRService.utilities.isConsole(d))
          return 'Dispatch console' + x;
        else if (QDRService.utilities.isArtemis(d))
          return 'Broker - Artemis' + x;
        else if (d.properties.product == 'qpid-cpp')
          return 'Broker - qpid-cpp' + x;
        else if (d.cdir === 'in')
          return 'Sender' + x;
        else if (d.cdir === 'out')
          return 'Receiver' + x;
        else if (d.cdir === 'both')
          return 'Sender/Receiver' + x;
        else if (d.nodeType === 'normal')
          return 'client' + x;
        else if (d.nodeType === 'on-demand')
          return 'broker';
        else if (d.properties.product) {
          return d.properties.product;
        }
        else {
          return '';
        }
      };

      let showClientTooltip = function (d, event) {
        let type = generateTitle(d);
        let title = '<table class="popupTable"><tr><td>Type</td><td>' + type + '</td></tr>';
        if (!d.normals || d.normals.length < 2)
          title += ('<tr><td>Host</td><td>' + d.host + '</td></tr>');
        title += '</table>';
        showToolTip(title, event);
      };

      let showRouterTooltip = function (d, event) {
        QDRService.management.topology.ensureEntities(d.key, [
          {entity: 'listener', attrs: ['role', 'port', 'http']},
          {entity: 'router', attrs: ['name', 'version', 'hostName']}
        ], function () {
          // update all the router title text
          let nodes = QDRService.management.topology.nodeInfo();
          let node = nodes[d.key];
          let listeners = node['listener'];
          let router = node['router'];
          let r = QDRService.utilities.flatten(router.attributeNames, router.results[0]);
          let title = '<table class="popupTable">';
          title += ('<tr><td>Router</td><td>' + r.name + '</td></tr>');
          if (r.hostName)
            title += ('<tr><td>Host Name</td><td>' + r.hostHame + '</td></tr>');
          title += ('<tr><td>Version</td><td>' + r.version + '</td></tr>');
          let ports = [];
          for (let l=0; l<listeners.results.length; l++) {
            let listener = QDRService.utilities.flatten(listeners.attributeNames, listeners.results[l]);
            if (listener.role === 'normal') {
              ports.push(listener.port+'');
            }
          }
          if (ports.length > 0) {
            title += ('<tr><td>Ports</td><td>' + ports.join(', ') + '</td></tr>');
          }
          title += '</table>';
          showToolTip(title, event);
        });
      };
      let showToolTip = function (title, event) {
        // show the tooltip
        $timeout ( function () {
          $scope.trustedpopoverContent = $sce.trustAsHtml(title);
          displayTooltip(event);
        });
      };

      let displayTooltip = function (event) {
        $timeout( function () {
          let top = $('#topology').offset().top - 5;
          let width = $('#topology').width();
          d3.select('#popover-div')
            .style('visibility', 'hidden')
            .style('display', 'block')
            .style('left', (event.pageX+5)+'px')
            .style('top', (event.pageY-top)+'px');
          let pwidth = $('#popover-div').width();
          d3.select('#popover-div')
            .style('visibility', 'visible')
            .style('left',(Math.min(width-pwidth, event.pageX+5) + 'px'));
        });
      };

      function nextHop(thisNode, d, cb) {
        if ((thisNode) && (thisNode != d)) {
          let target = findNextHopNode(thisNode, d);
          //QDR.log.debug("highlight link from node ");
          //console.dump(nodeFor(selected_node.name));
          //console.dump(target);
          if (target) {
            let hnode = nodeFor(thisNode.name);
            let hlLink = linkFor(hnode, target);
            //QDR.log.debug("need to highlight");
            //console.dump(hlLink);
            if (hlLink) {
              if (cb) {
                cb(hlLink, hnode, target);
              } else {
                hlLink['highlighted'] = true;
                hnode['highlighted'] = true;
              }
            }
            else
              target = null;
          }
          nextHop(target, d, cb);
        }
        if (thisNode == d && !cb) {
          let hnode = nodeFor(thisNode.name);
          hnode['highlighted'] = true;
        }
      }

      function hasChanged() {
        // Don't update the underlying topology diagram if we are adding a new node.
        // Once adding is completed, the topology will update automatically if it has changed
        let nodeInfo = QDRService.management.topology.nodeInfo();
        // don't count the nodes without connection info
        let cnodes = Object.keys(nodeInfo).filter ( function (node) {
          return (nodeInfo[node]['connection']);
        });
        let routers = nodes.filter( function (node) {
          return node.nodeType === 'inter-router';
        });
        if (routers.length > cnodes.length) {
          return -1;
        }


        if (cnodes.length != Object.keys(savedKeys).length) {
          return cnodes.length > Object.keys(savedKeys).length ? 1 : -1;
        }
        // we may have dropped a node and added a different node in the same update cycle
        for (let i=0; i<cnodes.length; i++) {
          let key = cnodes[i];
          // if this node isn't in the saved node list
          if (!savedKeys.hasOwnProperty(key))
            return 1;
          // if the number of connections for this node chaanged
          if (!nodeInfo[key]['connection'])
            return -1;
          if (nodeInfo[key]['connection'].results.length != savedKeys[key]) {
            return -1;
          }
        }
        return 0;
      }

      function saveChanged() {
        savedKeys = {};
        let nodeInfo = QDRService.management.topology.nodeInfo();
        // save the number of connections per node
        for (let key in nodeInfo) {
          if (nodeInfo[key]['connection'])
            savedKeys[key] = nodeInfo[key]['connection'].results.length;
        }
      }
      // we are about to leave the page, save the node positions
      $rootScope.$on('$locationChangeStart', function() {
        //QDR.log.debug("locationChangeStart");
        savePositions();
      });
      // When the DOM element is removed from the page,
      // AngularJS will trigger the $destroy event on
      // the scope
      $scope.$on('$destroy', function() {
        //QDR.log.debug("scope on destroy");
        savePositions();
        QDRService.management.topology.setUpdateEntities([]);
        QDRService.management.topology.stopUpdating();
        QDRService.management.topology.delUpdatedAction('normalsStats');
        QDRService.management.topology.delUpdatedAction('topology');
        QDRService.management.topology.delUpdatedAction('connectionPopupHTML');

        d3.select('#SVG_ID').remove();
        window.removeEventListener('resize', resize);
        traffic.stop();
      });

      function handleInitialUpdate() {
        // we only need to update connections during steady-state
        QDRService.management.topology.setUpdateEntities(['connection']);
        // we currently have all entities available on all routers
        saveChanged();
        initForceGraph();
        // after the graph is displayed fetch all .router.node info. This is done so highlighting between nodes
        // doesn't incur a delay
        QDRService.management.topology.addUpdateEntities({entity: 'router.node', attrs: ['id','nextHop']});
        // call this function every time a background update is done
        QDRService.management.topology.addUpdatedAction('topology', function() {
          let changed = hasChanged();
          // there is a new node, we need to get all of it's entities before drawing the graph
          if (changed > 0) {
            QDRService.management.topology.delUpdatedAction('topology');
            animate = true;
            setupInitialUpdate();
          } else if (changed === -1) {
            // we lost a node (or a client), we can draw the new svg immediately
            animate = false;
            saveChanged();
            let nodeInfo = QDRService.management.topology.nodeInfo();
            initializeNodes(nodeInfo);

            let unknowns = [];
            initializeLinks(nodeInfo, unknowns);
            if (unknowns.length > 0) {
              resolveUnknowns(nodeInfo, unknowns);
            }
            else {
              force.nodes(nodes).links(links).start();
              restart();
            }
            //initForceGraph();
          } else {
            //QDR.log.debug("topology didn't change")
          }

        });
      }
      function setupInitialUpdate() {
        // make sure all router nodes have .connection info. if not then fetch any missing info
        QDRService.management.topology.ensureAllEntities(
          [{entity: 'connection'}],
          handleInitialUpdate);
      }
      if (!QDRService.management.connection.is_connected()) {
        // we are not connected. we probably got here from a bookmark or manual page reload
        QDR.redirectWhenConnected($location, 'topology');
        return;
      }

      let connectionPopupHTML = function (d) {
        let getConnsArray = function (d, conn) {
          let conns = [conn];
          if (d.cls === 'small') {
            conns = [];
            let normals = d.target.normals ? d.target.normals : d.source.normals;
            for (let n=0; n<normals.length; n++) {
              if (normals[n].resultIndex !== undefined) {
                conns.push(QDRService.utilities.flatten(onode['connection'].attributeNames,
                  onode['connection'].results[normals[n].resultIndex]));
              }
            }
          }
          return conns;
        };
        // construct HTML to be used in a popup when the mouse is moved over a link.
        // The HTML is sanitized elsewhere before it is displayed
        let linksHTML = function (onode, conn, d) {
          const max_links = 10;
          const fields = ['undelivered', 'unsettled', 'rejected', 'released', 'modified'];
          // local function to determine if a link's connectionId is in any of the connections
          let isLinkFor = function (connectionId, conns) {
            for (let c=0; c<conns.length; c++) {
              if (conns[c].identity === connectionId)
                return true;
            }
            return false;
          };
          let fnJoin = function (ar, sepfn) {
            let out = '';
            out = ar[0];
            for (let i=1; i<ar.length; i++) {
              let sep = sepfn(ar[i]);
              out += (sep[0] + sep[1]);
            }
            return out;
          };
          let conns = getConnsArray(d, conn);
          // if the data for the line is from a client (small circle), we may have multiple connections
          // loop through all links for this router and accumulate those belonging to the connection(s)
          let nodeLinks = onode['router.link'];
          if (!nodeLinks)
            return '';
          let links = [];
          let hasAddress = false;
          for (let n=0; n<nodeLinks.results.length; n++) {
            let link = QDRService.utilities.flatten(nodeLinks.attributeNames, nodeLinks.results[n]);
            if (link.linkType !== 'router-control') {
              if (isLinkFor(link.connectionId, conns)) {
                if (link.owningAddr)
                  hasAddress = true;
                links.push(link);
              }
            }
          }
          // we may need to limit the number of links displayed, so sort descending by the sum of the field values
          links.sort( function (a, b) {
            let asum = a.undeliveredCount + a.unsettledCount + a.rejectedCount + a.releasedCount + a.modifiedCount;
            let bsum = b.undeliveredCount + b.unsettledCount + b.rejectedCount + b.releasedCount + b.modifiedCount;
            return asum < bsum ? 1 : asum > bsum ? -1 : 0;
          });
          let HTMLHeading = '<h5>Links</h5>';
          let HTML = '<table class="popupTable">';
          // copy of fields since we may be prepending an address
          let th = fields.slice();
          // convert to actual attribute names
          let td = fields.map( function (f) {return f + 'Count';});
          th.unshift('dir');
          td.unshift('linkDir');
          // add an address field if any of the links had an owningAddress
          if (hasAddress) {
            th.unshift('address');
            td.unshift('owningAddr');
          }
          HTML += ('<tr class="header"><td>' + th.join('</td><td>') + '</td></tr>');
          // add rows to the table for each link
          for (let l=0; l<links.length; l++) {
            if (l>=max_links) {
              HTMLHeading = `<h4>Top ${max_links} Links</h4>`;
              break;
            }
            let link = links[l];
            let vals = td.map( function (f) {
              if (f === 'owningAddr') {
                let identity = QDRService.utilities.identity_clean(link.owningAddr);
                return QDRService.utilities.addr_text(identity);
              }
              return link[f];
            });
            let joinedVals = fnJoin(vals, function (v1) {
              return ['</td><td' + (isNaN(+v1) ? '': ' align="right"') + '>', QDRService.utilities.pretty(v1 || '0')];
            });
            HTML += `<tr><td> ${joinedVals} </td></tr>`;
          }
          HTML += '</table>';
          return HTMLHeading + HTML;
        };
        let left = d.left ? d.source : d.target;
        // left is the connection with dir 'in'
        let right = d.left ? d.target : d.source;
        let onode = QDRService.management.topology.nodeInfo()[left.key];
        let connSecurity = function (conn) {
          if (!conn.isEncrypted)
            return 'no-security';
          if (conn.sasl === 'GSSAPI')
            return 'Kerberos';
          return conn.sslProto + '(' + conn.sslCipher + ')';
        };
        let connAuth = function (conn) {
          if (!conn.isAuthenticated)
            return 'no-auth';
          let sasl = conn.sasl;
          if (sasl === 'GSSAPI')
            sasl = 'Kerberos';
          else if (sasl === 'EXTERNAL')
            sasl = 'x.509';
          else if (sasl === 'ANONYMOUS')
            return 'anonymous-user';
          if (!conn.user)
            return sasl;
          return conn.user + '(' + sasl + ')';
        };
        let connTenant = function (conn) {
          if (!conn.tenant) {
            return '';
          }
          if (conn.tenant.length > 1)
            return conn.tenant.replace(/\/$/, '');
        };
        // loop through all the connections for left, and find the one for right
        let rightIndex = onode['connection'].results.findIndex( function (conn) {
          return QDRService.utilities.valFor(onode['connection'].attributeNames, conn, 'container') === right.routerId;
        });
        if (rightIndex < 0) {
          // we have a connection to a client/service
          rightIndex = +left.resultIndex;
        }
        if (isNaN(rightIndex)) {
          // we have a connection to a console
          rightIndex = +right.resultIndex;
        }
        let HTML = '';
        if (rightIndex >= 0) {
          let conn = onode['connection'].results[rightIndex];
          conn = QDRService.utilities.flatten(onode['connection'].attributeNames, conn);
          let conns = getConnsArray(d, conn);
          if (conns.length === 1) {
            HTML += '<h5>Connection'+(conns.length > 1 ? 's' : '')+'</h5>';
            HTML += '<table class="popupTable"><tr class="header"><td>Security</td><td>Authentication</td><td>Tenant</td><td>Host</td>';

            for (let c=0; c<conns.length; c++) {
              HTML += ('<tr><td>' + connSecurity(conns[c]) + '</td>');
              HTML += ('<td>' + connAuth(conns[c]) + '</td>');
              HTML += ('<td>' + (connTenant(conns[c]) || '--') + '</td>');
              HTML += ('<td>' + conns[c].host + '</td>');
              HTML += '</tr>';
            }
            HTML += '</table>';
          }
          HTML += linksHTML(onode, conn, d);
        }
        return HTML;
      };


      animate = true;
      setupInitialUpdate();
      QDRService.management.topology.startUpdating(false);

    }
  ]);

  return QDR;

}(QDR || {}));
