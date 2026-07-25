import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./index.css";
import App from "./App";
import Home from "./pages/Home";
import Members from "./pages/Members";
import Events from "./pages/Events";
import Proposals from "./pages/Proposals";
import Notices from "./pages/Notices";
import Surveys from "./pages/Surveys";
import Agent from "./pages/Agent";
import SettingsPage from "./pages/Settings";

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <Home /> },
        { path: "members", element: <Members /> },
        { path: "events", element: <Events /> },
        { path: "proposals", element: <Proposals /> },
        { path: "notices", element: <Notices /> },
        { path: "surveys", element: <Surveys /> },
        { path: "agent", element: <Agent /> },
        { path: "settings", element: <SettingsPage /> },
      ],
    },
  ],
  { basename: "/app" }
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>
);
