import { Link, NavLink, Route, Routes } from "react-router-dom";
import Home from "./pages/Home";
import Dataset from "./pages/Dataset";
import Run from "./pages/Run";
import Runs from "./pages/Runs";
import Settings from "./pages/Settings";
import Compare from "./pages/Compare";
import Comparison from "./pages/Comparison";

export default function App() {
  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">RNA-seq Analysis</Link>
        <nav>
          <NavLink to="/" end>New analysis</NavLink>
          <NavLink to="/runs">My analyses</NavLink>
          <NavLink to="/compare">Compare studies</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/datasets/:uploadId" element={<Dataset />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:jobId" element={<Run />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/compare/:id" element={<Comparison />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<p>Page not found. <Link to="/">Start a new analysis</Link>.</p>} />
        </Routes>
      </main>
    </>
  );
}
